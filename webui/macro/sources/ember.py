# ============================================================
# [모듈 개요] Ember 발전 믹스 소스 (elec_mix, 복합값 payload)
#
# Ember Energy의 전원별 발전량·비중을 받아 CONTRACT.md 3장 복합값
# (`value=None`, `payload={"items":[...], "total_twh":...}`)으로 저장합니다.
# CC BY 4.0. 연간(Y)과 월간(M)을 모두 수집합니다.
#
# [4단 경로 — 모두 실측] (2026-09-20, EMBER_API_KEY 발급 후 재실측)
#  1) API (EMBER_API_KEY 있을 때)
#     GET https://api.ember-energy.org/v1/electricity-generation/{yearly|monthly}
#     파라미터(문서 https://api.ember-energy.org/v1/openapi.json 로 확정):
#       entity / **entity_code**(3자 ISO, 콤마 구분) / is_aggregate_entity /
#       start_date / end_date / series / is_aggregate_series / api_key
#     응답 스키마(OpenAPI `GenerationResponse`):
#       {"stats": {...},
#        "data": [{"entity","entity_code","is_aggregate_entity","date",
#                  "series","is_aggregate_series","generation_twh",
#                  "share_of_generation_pct"}, ...]}
#     ※ 인증(실측): OpenAPI가 `api_key`를 **쿼리 파라미터**로 선언하고, 헤더
#       `X-API-Key`만 보내면 키가 유효해도 403 `{"detail":"No API key set"}`이다
#       → 쿼리를 **먼저** 쓰고, 거부(401/403)를 만나면 다른 방식으로 1회만 넘어간
#       뒤 통한 방식을 `ctx.extra["ember_api_auth"]`에 기억해 이후 요청에서 재시도
#       낭비를 없앤다. 키 없이 호출하면 `403 {"detail":"No API key set"}`.
#     ※ 유로존(실측): `/v1/options/electricity-generation/yearly/entity_code`가
#       주는 209개 코드는 3자 ISO뿐이라 유로존 코드가 없고, 같은 경로의
#       `.../entity` 224개 옵션에 집계 엔티티 **`EU`**(entity_code=null,
#       is_aggregate_entity=true)가 있다. 그런데 `entity`와 `entity_code`를 한
#       요청에 같이 넣으면 AND로 걸려 0행이 온다 → ISO3 19개국은 `entity_code`
#       요청, 유로존은 `entity=EU` 요청으로 **나눠서** 보낸다.
#  2) API 응답에 없는 국가만 **공개 CSV로 보충** (flags: fallback_source)
#     집계 엔티티 요청이 실패하거나 어떤 국가가 응답에 없으면 그 국가만 아래 공개
#     CSV에서 채운다. 로그: `[ember] API 미제공 N개국 → 공개 CSV 보충: EU`.
#     단 **다른 해상도를 API가 준 국가는 제외**한다 — Ember에 ID·SA 월간 시계열이
#     아예 없어(실측) CSV에도 없고, 28MB 월간 CSV를 매 실행 헛되게 받게 된다.
#  3) 키가 없거나 API 요청이 실패하면 **무키 공개 CSV** 전체 경로 (실측 확인)
#     연간 https://files.ember-energy.org/public-downloads/generation/outputs/
#           release_generation_yearly_global.csv   (HTTP 200, 16,079,748 bytes)
#     월간 .../release_generation_monthly_global.csv                (HTTP 200)
#     열: Area, ISO 3 code, Year(연간) 또는 Date(월간, `YYYY-MM-01`), Area type,
#         Electricity source, Is aggregated source, Generation (TWh),
#         Share of generation (%), ...
#     전원 코드 10종 실측: Bioenergy, Coal, Gas, Hydro, Net imports, Nuclear,
#         Other fossil, Other renewables, Solar, Wind
#     G20 19개국 전부 + **유로존은 Area="EU"**(ISO 3 code 비어 있음)로 존재.
#     이 경로(2·3) 관측치에는 flags에 `fallback_source`를 붙입니다.
#  4) CSV까지 실패하면 owid.elec_mix_from_owid(연간만, flags fallback_source)
#
# [Net imports 제외] Ember의 "Net imports"는 발전이 아니라 수입(음수도 가능)
# 이므로 발전 믹스 items에서 뺍니다. 남은 전원 비중 합은 KOR 2025에서 100.0%.
#
# 국가별 실패 격리: 경로마다 요청은 1~2건(전 국가 묶음)이므로 경로 실패는 다음
# 경로로 내려가고, 마지막 경로까지 실패하면 대상 국가 전부에 오류를 기록합니다.
# 개별 국가가 응답에 없으면 조용히 건너뜁니다.
# ============================================================
from __future__ import annotations

import io
import os
from datetime import date
from typing import Any

import pandas as pd

from macro.schema import Observation
from macro.sources import owid as owid_source
from macro.sources.base import (
    CollectContext,
    country_code,
    field_of,
    find_indicator,
    iter_countries,
    safe_float,
)

SOURCE_NAME = "ember"
CADENCE = "monthly"

API_KEY_ENV = "EMBER_API_KEY"
API_BASE = "https://api.ember-energy.org/v1/electricity-generation"

# 인증 방식. OpenAPI가 `api_key` 쿼리를 공식으로 선언하고 헤더 `X-API-Key`는
# 403으로 거부되므로(실측) 쿼리가 먼저다. 통한 방식은 ctx.extra에 기억한다.
AUTH_QUERY = "query"
AUTH_HEADER = "header"
AUTH_EXTRA_KEY = "ember_api_auth"
PUBLIC_CSV = {
    "yearly": (
        "https://files.ember-energy.org/public-downloads/generation/outputs/"
        "release_generation_yearly_global.csv"
    ),
    "monthly": (
        "https://files.ember-energy.org/public-downloads/generation/outputs/"
        "release_generation_monthly_global.csv"
    ),
}
DOCS_URL = "https://ember-energy.org/data/yearly-electricity-data/"

# 16MB 안팎의 CSV라 기본 타임아웃(30초)보다 넉넉히 준다.
DOWNLOAD_TIMEOUT = 180

# ctx.since가 없을 때: 연간은 이 연도부터, 월간은 최근 이 개월 수만.
DEFAULT_START_YEAR = 2000
DEFAULT_MONTHS = 36

# Ember 전원 코드 → 한글 라벨. "Net imports"는 발전이 아니라 제외한다.
FUEL_LABELS: dict[str, str] = {
    "Coal": "석탄",
    "Gas": "가스",
    "Nuclear": "원자력",
    "Hydro": "수력",
    "Wind": "풍력",
    "Solar": "태양광",
    "Bioenergy": "바이오",
    "Other fossil": "기타화석",
    "Other renewables": "기타재생",
}
EXCLUDED_SERIES = ("Net imports",)

METHOD_API = "Ember 전원별 발전량(비아집계 전원)에서 비중·발전량을 그대로 사용 (API)"
METHOD_CSV = (
    "Ember 공개 연간/월간 발전 데이터셋(CSV, 무키 다운로드)에서 전원별 비중·발전량을 "
    "그대로 사용. Net imports(순수입)는 발전이 아니라 제외"
)
METHOD_OWID = (
    "Ember 접근 실패로 OWID 에너지 CSV의 *_share_elec 열에서 발전 믹스를 대체 구성 "
    "(biofuel_share_elec는 other_renewables_share_elec에 포함되어 제외)"
)

METHOD_CSV_SUPPLEMENT = (
    "Ember 공개 CSV(API 미제공 국가 보충) — API가 유로존 같은 집계 엔티티를 응답에 "
    "담지 않아 해당 국가만 무키 공개 연간/월간 CSV에서 채움. Net imports(순수입)는 제외"
)

SUPPORTED: tuple[str, ...] = ("elec_mix",)


# ====================================================================== 공개 함수
def api_key() -> str:
    """`EMBER_API_KEY` 환경변수 (없으면 빈 문자열)."""
    return (os.environ.get(API_KEY_ENV) or "").strip()


def label_for(series_name: str) -> str | None:
    """Ember 전원 코드 → 한글 라벨. 제외 대상/미등록이면 None.

    미등록 전원은 영문 원문을 그대로 라벨로 쓴다(fail-open).
    """
    name = (series_name or "").strip()
    if not name or name in EXCLUDED_SERIES:
        return None
    return FUEL_LABELS.get(name, name)


def build_payload(rows: list[tuple[str, float | None, float | None]]) -> dict[str, Any] | None:
    """`[(전원명, 비중%, 발전량TWh)]` → elec_mix payload. 항목이 없으면 None."""
    items: list[dict[str, Any]] = []
    total = 0.0
    has_twh = False
    for name, share, twh in rows:
        label = label_for(name)
        if label is None:
            continue
        if share is None and twh is None:
            continue
        item: dict[str, Any] = {"label": label, "value": round(share, 4) if share is not None else None}
        if twh is not None:
            item["twh"] = round(twh, 4)
            total += twh
            has_twh = True
        items.append(item)
    if not items:
        return None
    items.sort(key=lambda it: (it["value"] is None, -(it["value"] or 0.0)))
    payload: dict[str, Any] = {"items": items}
    if has_twh:
        payload["total_twh"] = round(total, 4)
    return payload


def collect(
    countries: list[Any],
    indicators: list[Any],
    ctx: CollectContext,
) -> list[Observation]:
    """Ember 발전 믹스를 수집한다 (CONTRACT 7장).

    EMBER_API_KEY가 있으면 API를 쓰고 API가 응답에 담지 않은 국가(유로존 등)만
    공개 CSV로 보충하며, 키가 없거나 API 요청이 실패하면 무키 공개 CSV 전체
    경로를, 그마저 실패하면 OWID로 폴백한다. 연간(Y)과 월간(M)을 모두 만든다.
    """
    if not _is_wanted(indicators, "elec_mix"):
        return []
    ind = find_indicator(indicators, "elec_mix") or _FallbackIndicator("elec_mix")
    targets = _targets(countries, ind)
    if not targets:
        ctx.log(f"[{SOURCE_NAME}] 대상 국가 없음 — 건너뜀")
        return []

    key = api_key()
    if not key:
        ctx.log(
            f"[{SOURCE_NAME}] {API_KEY_ENV} 없음 — 무키 공개 CSV로 수집합니다 "
            f"(flags: fallback_source, {DOCS_URL})"
        )

    obs: list[Observation] = []
    # API가 한 해상도라도 준 국가는 다른 해상도가 비어도 원천 자체가 없는 것이므로
    # (예: Ember는 ID·SA 월간 시계열이 없다 — 실측) CSV 보충 대상에서 뺀다.
    api_served: set[str] = set()
    for resolution in ("yearly", "monthly"):
        try:
            got = _collect_resolution(ctx, targets, resolution, key, api_served)
        except Exception as exc:  # noqa: BLE001 - 해상도 단위 실패 격리
            ctx.log(f"[{SOURCE_NAME}] {resolution} 수집 실패: {exc}")
            got = []
        obs.extend(got)

    if not obs:
        obs = _owid_fallback(ctx, countries, ind, targets)
    if not obs:
        for iso in sorted(set(targets.values())):
            ctx.record_error(SOURCE_NAME, iso, "elec_mix 수집 실패 (API·공개 CSV·OWID 모두)")
    _log_coverage(ctx, targets, obs)
    return obs


# ====================================================================== 내부 구현
def _targets(countries: list[Any], ind: Any) -> dict[str, str]:
    """`{(ISO3 또는 Area 이름): iso}`. registry의 codes["ember"]를 흡수한다."""
    out: dict[str, str] = {}
    for country in iter_countries(countries, ind, SOURCE_NAME):
        iso = str(field_of(country, "iso") or "").upper()
        if not iso:
            continue
        code = country_code(country, "ember")
        if isinstance(code, dict):
            for value in (code.get("iso3"), code.get("name")):
                if value:
                    out[str(value)] = iso
        elif code:
            out[str(code)] = iso
    return out


def _collect_resolution(
    ctx: CollectContext,
    targets: dict[str, str],
    resolution: str,
    key: str,
    api_served: set[str],
) -> list[Observation]:
    """연간/월간 한 해상도를 수집한다.

    키가 있으면 API를 쓰고, API가 응답에 담지 않은 국가(유로존 등)만 공개 CSV로
    보충한다. API 요청 자체가 실패하면 기존처럼 공개 CSV 전체 경로로 내려간다.
    """
    if key:
        try:
            obs = _collect_api(ctx, targets, resolution, key)
        except Exception as exc:  # noqa: BLE001 - API 실패는 CSV로 내려간다
            ctx.log(f"[{SOURCE_NAME}] API {resolution} 실패({exc}) — 공개 CSV로 폴백")
        else:
            supplement = _csv_supplement(ctx, targets, resolution, obs, api_served)
            api_served.update(ob.iso for ob in obs)
            return obs + supplement
    return _collect_public_csv(ctx, targets, resolution)


def _collect_api(
    ctx: CollectContext,
    targets: dict[str, str],
    resolution: str,
    key: str,
) -> list[Observation]:
    """Ember API → Observation 목록.

    `entity`와 `entity_code`를 한 요청에 같이 넣으면 AND로 걸려 0행이 오므로
    (2026-09-20 실측) ISO3 국가는 `entity_code`로 한 번, 유로존처럼 ISO3 코드가
    없는 집계 엔티티는 `entity`로 한 번 더 요청해 합친다. 집계 엔티티 요청이
    실패해도 ISO3 결과는 살리고, 빠진 국가는 호출자가 공개 CSV로 보충한다.
    """
    iso3s, names = _api_entities(targets)
    url = f"{API_BASE}/{resolution}"
    base: dict[str, Any] = {
        "is_aggregate_series": "false",
        "start_date": _api_start(ctx, resolution),
    }
    rows: list[dict[str, Any]] = []
    served = False
    if iso3s:
        # 이 요청이 실패하면 예외를 올려 공개 CSV 전체 폴백으로 내려간다.
        params = {**base, "entity_code": ",".join(iso3s)}
        doc = _api_fetch(ctx, url, params, key, resolution, "code")
        rows.extend(doc.get("data") or [])
        served = True
    if names:
        try:
            params = {**base, "entity": ",".join(names)}
            doc = _api_fetch(ctx, url, params, key, resolution, "entity")
        except Exception as exc:  # noqa: BLE001 - 집계 엔티티 실패는 CSV 보충에 맡긴다
            ctx.log(
                f"[{SOURCE_NAME}] API {resolution} 집계 엔티티({','.join(names)}) 실패({exc})"
            )
        else:
            rows.extend(doc.get("data") or [])
            served = True
    if not served:
        raise RuntimeError("API 요청이 모두 실패")
    return _obs_from_api({"data": rows}, targets, resolution, url)


def _api_entities(targets: dict[str, str]) -> tuple[list[str], list[str]]:
    """대상 코드를 API 파라미터로 나눈다 → (`entity_code`용 ISO3, `entity`용 이름).

    국가마다 registry `codes.ember`가 이름·iso3를 함께 주므로, 3자 ISO 코드가
    있으면 그쪽을 쓰고(중복 요청 방지) 없으면 이름으로 요청한다. 유로존은
    entity_code 옵션에 없고 집계 엔티티 이름 `EU`만 있다(실측).
    """
    by_iso: dict[str, list[str]] = {}
    for code, iso in targets.items():
        by_iso.setdefault(iso, []).append(code)
    iso3s: set[str] = set()
    names: set[str] = set()
    for codes in by_iso.values():
        got = sorted(c for c in codes if _is_api_iso3(c))
        if got:
            iso3s.add(got[0])
            continue
        names.update(c for c in codes if c)
    return sorted(iso3s), sorted(names)


def _is_api_iso3(code: str) -> bool:
    """API `entity_code`로 쓸 수 있는 3자 ISO 코드인지 (EA20 같은 값은 제외)."""
    return len(code) == 3 and code.isalpha() and code.isupper()


def _api_fetch(
    ctx: CollectContext,
    url: str,
    params: dict[str, Any],
    key: str,
    resolution: str,
    tag: str,
) -> dict[str, Any]:
    """API GET 1회 + 원본 보존. HTTP 4xx/5xx는 예외로 올린다."""
    resp = _api_get(ctx, url, params, key)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:120]}")
    ctx.save_raw(SOURCE_NAME, f"api_generation_{resolution}_{tag}", resp.text)
    return resp.json() or {}


def _api_get(
    ctx: CollectContext,
    url: str,
    params: dict[str, Any],
    key: str,
) -> Any:
    """인증 방식을 기억하며 API를 호출한다 (쿼리 `api_key` 우선).

    OpenAPI가 쿼리 파라미터를 공식으로 선언하고 헤더 `X-API-Key`는 403으로
    거부되므로(실측) 쿼리를 먼저 보낸다. 거부(401/403)를 만나면 다른 방식으로
    1회 넘어가고, 통한 방식을 `ctx.extra`에 기억해 같은 실행의 다음 요청부터는
    한 번만 보낸다(재시도 낭비 제거).
    """
    remembered = ctx.extra.get(AUTH_EXTRA_KEY)
    modes = [remembered] if remembered in (AUTH_QUERY, AUTH_HEADER) else []
    modes += [m for m in (AUTH_QUERY, AUTH_HEADER) if m not in modes]
    resp = None
    for idx, mode in enumerate(modes):
        if mode == AUTH_QUERY:
            resp = ctx.get(url, params={**params, "api_key": key})
        else:
            resp = ctx.get(url, params=dict(params), headers={"X-API-Key": key})
        if resp.status_code not in (401, 403):
            ctx.extra[AUTH_EXTRA_KEY] = mode
            return resp
        if idx + 1 < len(modes):
            ctx.log(
                f"[{SOURCE_NAME}] {mode} 인증 거부({resp.status_code}) — "
                f"{modes[idx + 1]} 방식으로 1회 재시도"
            )
    return resp


def _obs_from_api(
    doc: dict[str, Any],
    targets: dict[str, str],
    resolution: str,
    url: str,
) -> list[Observation]:
    """Ember API 응답(JSON) → elec_mix Observation 목록."""
    grouped: dict[tuple[str, str], list[tuple[str, float | None, float | None]]] = {}
    for row in (doc or {}).get("data") or []:
        if row.get("is_aggregate_series"):
            continue
        iso = targets.get(str(row.get("entity_code") or "")) or targets.get(
            str(row.get("entity") or "")
        )
        period = _normalize_period(str(row.get("date") or ""), resolution)
        if not iso or not period:
            continue
        grouped.setdefault((iso, period), []).append(
            (
                str(row.get("series") or ""),
                safe_float(row.get("share_of_generation_pct")),
                safe_float(row.get("generation_twh")),
            )
        )
    return _build_obs(grouped, resolution, METHOD_API, url, flags=[])


def _csv_supplement(
    ctx: CollectContext,
    targets: dict[str, str],
    resolution: str,
    obs: list[Observation],
    api_served: set[str],
) -> list[Observation]:
    """API 응답에 없는 국가만 공개 CSV에서 보충한다 (flags: fallback_source).

    API는 유로존 집계 엔티티를 `entity_code`로 주지 않으므로, 집계 엔티티 요청이
    막히면 유로존 발전 믹스가 영구히 멈춘다. 그 안전망이다.

    `api_served`(앞선 해상도에서 API가 값을 준 국가)는 제외한다 — Ember에 그
    해상도의 시계열이 아예 없는 경우(ID·SA 월간)라 CSV에도 없고, 28MB 월간 CSV를
    매번 헛되게 내려받게 된다(실측).
    """
    absent = set(targets.values()) - {ob.iso for ob in obs}
    skipped = sorted(absent & api_served)
    if skipped:
        ctx.log(
            f"[{SOURCE_NAME}] {resolution} 원천 없음(다른 해상도는 API가 제공) — "
            f"CSV 보충 생략: {','.join(skipped)}"
        )
    missing = sorted(absent - api_served)
    if not missing:
        return []
    ctx.log(
        f"[{SOURCE_NAME}] API 미제공 {len(missing)}개국 → 공개 CSV 보충: "
        f"{','.join(missing)} ({resolution})"
    )
    wanted = set(missing)
    subset = {code: iso for code, iso in targets.items() if iso in wanted}
    try:
        return _collect_public_csv(
            ctx, subset, resolution, method=METHOD_CSV_SUPPLEMENT, raw_tag="supplement"
        )
    except Exception as exc:  # noqa: BLE001 - 보충 실패로 API 결과를 버리지 않는다
        ctx.log(f"[{SOURCE_NAME}] 공개 CSV 보충 실패({resolution}): {exc}")
        return []


def _collect_public_csv(
    ctx: CollectContext,
    targets: dict[str, str],
    resolution: str,
    method: str = METHOD_CSV,
    raw_tag: str = "g20",
) -> list[Observation]:
    """무키 공개 CSV 1회 다운로드 → Observation 목록 (flags: fallback_source).

    CSV는 해상도당 한 번만 내려받아 `ctx.extra`에 캐시한다 → 전체 폴백과 국가별
    보충이 같은 실행에서 겹쳐도 다운로드는 해상도당 1회다.
    """
    url = PUBLIC_CSV[resolution]
    cache_key = f"ember_csv_{resolution}"
    df = ctx.extra.get(cache_key)
    if not isinstance(df, pd.DataFrame):
        resp = ctx.get(url, timeout=DOWNLOAD_TIMEOUT)
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}")
        df = pd.read_csv(io.StringIO(resp.text))
        ctx.extra[cache_key] = df
        ctx.log(f"[{SOURCE_NAME}] 공개 CSV({resolution}) {len(resp.text):,}바이트")

    date_col = "Year" if resolution == "yearly" else "Date"
    if date_col not in df.columns:
        raise RuntimeError(f"CSV에 {date_col} 열이 없음")
    iso_series = df["ISO 3 code"].fillna("").astype(str)
    match = iso_series.isin(targets.keys()) | df["Area"].astype(str).isin(targets.keys())
    sub = df[match & (df["Is aggregated source"].astype(str).str.lower() == "false")]

    grouped: dict[tuple[str, str], list[tuple[str, float | None, float | None]]] = {}
    for _, row in sub.iterrows():
        iso = targets.get(str(row["ISO 3 code"] or "").strip()) or targets.get(
            str(row["Area"]).strip()
        )
        period = _normalize_period(str(row[date_col]), resolution)
        if not iso or not period or not _in_window(ctx, period, resolution):
            continue
        grouped.setdefault((iso, period), []).append(
            (
                str(row["Electricity source"]),
                safe_float(row.get("Share of generation (%)")),
                safe_float(row.get("Generation (TWh)")),
            )
        )
    ctx.save_raw(SOURCE_NAME, f"public_csv_{resolution}_{raw_tag}", sub.to_csv(index=False))
    return _build_obs(grouped, resolution, method, url, flags=["fallback_source"])


def _build_obs(
    grouped: dict[tuple[str, str], list[tuple[str, float | None, float | None]]],
    resolution: str,
    method: str,
    url: str,
    flags: list[str],
) -> list[Observation]:
    """`{(iso, 기간): [(전원, 비중, TWh)]}` → 복합값 Observation 목록."""
    freq = "Y" if resolution == "yearly" else "M"
    obs: list[Observation] = []
    for (iso, period), rows in sorted(grouped.items()):
        payload = build_payload(rows)
        if payload is None:
            continue
        obs.append(
            Observation(
                indicator="elec_mix",
                iso=iso,
                freq=freq,
                period=period,
                value=None,
                payload=payload,
                unit="pct_share",
                source=SOURCE_NAME,
                series_id=f"electricity-generation/{resolution}",
                source_url=url,
                method=method,
                flags=list(flags),
            )
        )
    return obs


def _owid_fallback(
    ctx: CollectContext,
    countries: list[Any],
    ind: Any,
    targets: dict[str, str],
) -> list[Observation]:
    """Ember가 전부 막혔을 때 OWID 비중 열로 연간 elec_mix를 만든다."""
    try:
        df = owid_source.load_dataframe(ctx)
    except Exception as exc:  # noqa: BLE001 - 마지막 폴백 실패
        ctx.log(f"[{SOURCE_NAME}] OWID 폴백도 실패: {exc}")
        return []
    ctx.log(f"[{SOURCE_NAME}] OWID 폴백으로 elec_mix 구성 (연간만)")
    obs: list[Observation] = []
    for country in iter_countries(countries, ind, SOURCE_NAME):
        iso = str(field_of(country, "iso") or "").upper()
        iso3 = owid_source.owid_iso3(country)
        if not iso or not iso3 or iso not in targets.values():
            continue
        try:
            got = owid_source.elec_mix_from_owid(df, iso3)
        except Exception as exc:  # noqa: BLE001 - 국가별 실패 격리
            ctx.record_error(SOURCE_NAME, iso, f"OWID 폴백 실패: {exc}")
            continue
        if got is None:
            continue
        year, payload = got
        obs.append(
            Observation(
                indicator="elec_mix",
                iso=iso,
                freq="Y",
                period=f"{year:04d}",
                value=None,
                payload=payload,
                unit="pct_share",
                source=owid_source.SOURCE_NAME,
                series_id="owid-energy-data/*_share_elec",
                source_url=owid_source.CSV_URL,
                method=METHOD_OWID,
                flags=["fallback_source", "estimated"],
            )
        )
    return obs


def _normalize_period(raw: str, resolution: str) -> str | None:
    """Ember 날짜를 CONTRACT 3장 기간으로 바꾼다 (연간 `YYYY`, 월간 `YYYY-MM`)."""
    s = (raw or "").strip()
    if resolution == "yearly":
        s = s[:4]
        return s if len(s) == 4 and s.isdigit() else None
    if len(s) >= 7 and s[4] == "-" and s[:4].isdigit() and s[5:7].isdigit():
        return s[:7]
    return None


def _api_start(ctx: CollectContext, resolution: str) -> str:
    """API `start_date` 파라미터 (연간 `YYYY`, 월간 `YYYY-MM`)."""
    since = ctx.since or _default_since(resolution)
    return since.strftime("%Y") if resolution == "yearly" else since.strftime("%Y-%m")


def _default_since(resolution: str) -> date:
    """ctx.since가 없을 때의 기본 시작일."""
    if resolution == "yearly":
        return date(DEFAULT_START_YEAR, 1, 1)
    today = date.today()
    total = today.year * 12 + (today.month - 1) - DEFAULT_MONTHS
    return date(total // 12, total % 12 + 1, 1)


def _in_window(ctx: CollectContext, period: str, resolution: str) -> bool:
    """공개 CSV는 전 기간을 담고 있으므로 수집 창 안의 기간만 남긴다."""
    since = ctx.since or _default_since(resolution)
    if resolution == "yearly":
        return int(period) >= since.year
    return period >= since.strftime("%Y-%m")


def _is_wanted(indicators: list[Any], indicator_id: str) -> bool:
    """요청 지표 목록이 비어 있으면 전 지원 지표, 아니면 목록에 있는 것만."""
    if not indicators:
        return True
    return find_indicator(indicators, indicator_id) is not None


class _FallbackIndicator:
    """registry 항목 없이 지표 id만 아는 경우의 최소 대역(iter_countries용)."""

    def __init__(self, indicator_id: str) -> None:
        self.id = indicator_id
        self.sources: list[dict[str, Any]] = []


def _log_coverage(
    ctx: CollectContext,
    targets: dict[str, str],
    obs: list[Observation],
) -> None:
    """값을 못 받은 국가를 한 줄로 알린다."""
    missing = sorted(set(targets.values()) - {ob.iso for ob in obs})
    if missing:
        ctx.log(f"[{SOURCE_NAME}] elec_mix 미수집 {len(missing)}개국: {','.join(missing)}")
