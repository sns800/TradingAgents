# ============================================================
# [모듈 개요] Ember 발전 믹스 소스 (elec_mix, 복합값 payload)
#
# Ember Energy의 전원별 발전량·비중을 받아 CONTRACT.md 3장 복합값
# (`value=None`, `payload={"items":[...], "total_twh":...}`)으로 저장합니다.
# CC BY 4.0. 연간(Y)과 월간(M)을 모두 수집합니다.
#
# [3단 경로 — 모두 실측] (2026-09-20)
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
#     ※ 인증: OpenAPI의 securitySchemes는 비어 있고 `api_key`가 **쿼리
#       파라미터**로 선언돼 있습니다. 제안서가 가리킨 헤더 `X-API-Key`를 먼저
#       쓰고, 401/403이면 쿼리 `api_key`로 1회 재시도합니다(키가 없어 실제
#       인증 성공은 확인하지 못함 — 보고서의 "미확정" 항목).
#     키 없이 호출하면 `403 {"detail":"No API key set"}` (실측).
#  2) 키가 없을 때의 **무키 공개 CSV** (실측 확인 — 이 경로가 기본)
#     연간 https://files.ember-energy.org/public-downloads/generation/outputs/
#           release_generation_yearly_global.csv   (HTTP 200, 16,079,748 bytes)
#     월간 .../release_generation_monthly_global.csv                (HTTP 200)
#     열: Area, ISO 3 code, Year(연간) 또는 Date(월간, `YYYY-MM-01`), Area type,
#         Electricity source, Is aggregated source, Generation (TWh),
#         Share of generation (%), ...
#     전원 코드 10종 실측: Bioenergy, Coal, Gas, Hydro, Net imports, Nuclear,
#         Other fossil, Other renewables, Solar, Wind
#     G20 19개국 전부 + **유로존은 Area="EU"**(ISO 3 code 비어 있음)로 존재 →
#     유로존까지 덮는 유일한 경로입니다. flags에 `fallback_source`를 붙입니다.
#  3) CSV까지 실패하면 owid.elec_mix_from_owid(연간만, flags fallback_source)
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

    EMBER_API_KEY가 있으면 API를, 없으면 무키 공개 CSV를 쓰고, 둘 다 실패하면
    OWID로 폴백한다. 연간(Y)과 월간(M)을 모두 만든다.
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
    for resolution in ("yearly", "monthly"):
        try:
            got = _collect_resolution(ctx, targets, resolution, key)
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
) -> list[Observation]:
    """연간/월간 한 해상도를 수집한다 (API 우선, 실패 시 공개 CSV)."""
    if key:
        try:
            return _collect_api(ctx, targets, resolution, key)
        except Exception as exc:  # noqa: BLE001 - API 실패는 CSV로 내려간다
            ctx.log(f"[{SOURCE_NAME}] API {resolution} 실패({exc}) — 공개 CSV로 폴백")
    return _collect_public_csv(ctx, targets, resolution)


def _collect_api(
    ctx: CollectContext,
    targets: dict[str, str],
    resolution: str,
    key: str,
) -> list[Observation]:
    """Ember API 1회 호출 → Observation 목록."""
    iso3s = sorted({c for c in targets if len(c) == 3 and c.isupper()})
    params: dict[str, Any] = {
        "entity_code": ",".join(iso3s),
        "is_aggregate_series": "false",
        "start_date": _api_start(ctx, resolution),
    }
    url = f"{API_BASE}/{resolution}"
    resp = ctx.get(url, params=params, headers={"X-API-Key": key})
    if resp.status_code in (401, 403):
        # OpenAPI는 api_key를 쿼리 파라미터로 선언한다 → 헤더가 안 먹으면 1회 재시도.
        ctx.log(f"[{SOURCE_NAME}] X-API-Key 헤더 거부({resp.status_code}) — api_key 쿼리로 재시도")
        resp = ctx.get(url, params={**params, "api_key": key})
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:120]}")
    doc = resp.json()
    ctx.save_raw(SOURCE_NAME, f"api_generation_{resolution}", resp.text)
    return _obs_from_api(doc, targets, resolution, url)


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


def _collect_public_csv(
    ctx: CollectContext,
    targets: dict[str, str],
    resolution: str,
) -> list[Observation]:
    """무키 공개 CSV 1회 다운로드 → Observation 목록 (flags: fallback_source)."""
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
    ctx.save_raw(SOURCE_NAME, f"public_csv_{resolution}_g20", sub.to_csv(index=False))
    return _build_obs(grouped, resolution, METHOD_CSV, url, flags=["fallback_source"])


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
