# ============================================================
# [모듈 개요] BIS Data Portal(SDMX v2) 정량 소스 — 정책금리·대미환율·주택가격지수
#
# 엔드포인트:
#   https://stats.bis.org/api/v2/data/dataflow/BIS/{FLOW}/1.0/{KEY}
#       ?format=csv&startPeriod=YYYY-MM-DD
# 한 요청에 여러 국가를 `+`로 묶을 수 있어(`D.KR+US+XM`) 지표당 1회 호출로
# 20개국을 모두 받는다. 국가를 묶으면 개별 국가 실패를 HTTP로 구분할 수 없으므로
# "응답에 없는 국가"를 실패 격리 대상으로 잡고(record_error 없이 로그), 요청
# 자체가 실패하면 대상 국가 전체에 record_error를 남긴다.
#
# [2026-09-20 실측으로 확정한 키 구조] — registry.yaml 반영 필요
#   WS_CBPOL  `D.{bis}`                    정책금리, 일별 기말(End of period)
#             컬럼: FREQ,REF_AREA,...,TIME_PERIOD,OBS_VALUE,OBS_STATUS,...
#             커버리지 16/20. AR(아르헨티나)는 없음. DE/FR/IT는 유로존 참조.
#   WS_XRU    `{FREQ}.{REF_AREA}.{CURRENCY}.{COLLECTION}`
#             ※ 제안서 부록 A의 `M.{ISO}.USD.A`는 **틀림**. 3번째 차원은 USD가
#               아니라 해당국 통화(KR→KRW, XM→EUR)이고, US만 CURRENCY=USD다.
#               COLLECTION: A=기간 평균, E=기말. CONTRACT 6장 `agg: last`에
#               맞춰 **E(기말)** 를 수집한다. 커버리지 20/20.
#   WS_SPP    `Q.{bis}.N.628`              명목 주택가격지수(2010=100), 분기
#             컬럼: FREQ,REF_AREA,VALUE,UNIT_MEASURE,...,TIME_PERIOD,OBS_VALUE
#             (VALUE=N 명목/R 실질, UNIT_MEASURE=628 지수). 커버리지 18/20 —
#             AR·SA 없음, JP는 1분기 지연.
#
# 유로존 BIS 코드는 `XM`. 정책금리 월별(freq M)은 집계 모듈(aggregate.py)이
# 아직 없어 이 모듈에서 월 마지막 일별 관측을 직접 취한다.
# ============================================================
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from macro.schema import Observation
from macro.sources.base import (
    CollectContext,
    country_code,
    field_of,
    find_indicator,
    indicator_unit,
    iter_countries,
    last_per_group,
    safe_float,
    sdmx_csv_to_rows,
)

SOURCE_NAME = "bis"
CADENCE = "daily"

API_BASE = "https://stats.bis.org/api/v2/data/dataflow/BIS"
# 사람이 열 수 있는 데이터 페이지 (Observation.source_url)
PORTAL_URL = {
    "WS_CBPOL": "https://data.bis.org/topics/CBPOL/data",
    "WS_XRU": "https://data.bis.org/topics/XRU/data",
    "WS_SPP": "https://data.bis.org/topics/SPP/data",
}
# since가 없을 때의 기본 시작일 (일별 정책금리 16년 ≈ 국가당 4천 관측).
DEFAULT_START = "2010-01-01"

# 증분 수집 룩백: 발표 지연·개정을 흡수하려고 since를 빈도별로 뒤로 민다.
LOOKBACK_DAYS = 7
LOOKBACK_MONTHS = 3
LOOKBACK_QUARTERS = 4

# 이 모듈이 담당하는 지표 (그 외 요청은 조용히 무시)
SUPPORTED = ("policy_rate", "fx_usd", "house_price_index")

# TIME_PERIOD 형식 (CONTRACT 3장 period 규칙). 어긋나는 행은 record_error 후 건너뛴다 —
# 잘못된 기간이 OBS sk(`D#...`)에 들어가면 시계열 Query 정렬·집계가 깨진다.
_PERIOD_RE = {
    "D": re.compile(r"^\d{4}-\d{2}-\d{2}$"),
    "M": re.compile(r"^\d{4}-\d{2}$"),
    "Q": re.compile(r"^\d{4}-Q[1-4]$"),
}
_PERIOD_HINT = {"D": "YYYY-MM-DD", "M": "YYYY-MM", "Q": "YYYY-Qn"}


def collect(countries: list, indicators: list, ctx: CollectContext) -> list[Observation]:
    """BIS SDMX v2에서 정책금리(D/M)·대미환율(M)·주택가격지수(Q)를 수집한다.

    국가별 실패 격리: 요청 실패는 대상 국가 전체에 record_error를 남기고 다음
    지표로 넘어간다. 응답에 값이 없는 국가(AR의 WS_SPP 등)는 오류가 아니라
    커버리지 문제이므로 로그만 남긴다.
    """
    out: list[Observation] = []

    if find_indicator(indicators, "policy_rate") is not None:
        out.extend(_collect_policy_rate(countries, indicators, ctx, _start_period(ctx.since, "D")))
    if find_indicator(indicators, "fx_usd") is not None:
        out.extend(_collect_fx_usd(countries, indicators, ctx, _start_period(ctx.since, "M")))
    if find_indicator(indicators, "house_price_index") is not None:
        out.extend(
            _collect_house_price(countries, indicators, ctx, _start_period(ctx.since, "Q"))
        )
    return out


# ====================================================================== 지표별
def _collect_policy_rate(
    countries: list, indicators: list, ctx: CollectContext, start: str
) -> list[Observation]:
    """WS_CBPOL `D.{bis}` → 일별(freq D) + 월말 기준값(freq M)."""
    indicator = find_indicator(indicators, "policy_rate")
    unit = indicator_unit(indicator, "%")
    targets = _bis_targets(countries, indicator)
    if not targets:
        return []

    key = "D." + "+".join(dict.fromkeys(targets.values()))
    rows = _fetch("WS_CBPOL", key, start, ctx, list(targets.values()))
    if rows is None:
        return []

    out: list[Observation] = []
    for iso, bis_code in _by_iso(targets):
        daily = _pairs(rows, bis_code, freq="D", ctx=ctx, iso=iso, flow="WS_CBPOL")
        if not daily:
            ctx.log(f"[{SOURCE_NAME}] {iso}: WS_CBPOL 데이터 없음 (BIS 미수록)")
            continue
        series_id = f"WS_CBPOL/1.0/D.{bis_code}"
        url = PORTAL_URL["WS_CBPOL"]
        for period, value in daily:
            out.append(
                Observation(
                    indicator="policy_rate",
                    iso=iso,
                    freq="D",
                    period=period,
                    value=value,
                    unit=unit,
                    source=SOURCE_NAME,
                    series_id=series_id,
                    source_url=url,
                    method="일별 정책금리 (BIS)",
                )
            )
        # 집계 모듈이 준비되기 전까지 월말 기준값을 이 모듈에서 만든다.
        for month, value in last_per_group(daily):
            out.append(
                Observation(
                    indicator="policy_rate",
                    iso=iso,
                    freq="M",
                    period=month,
                    value=value,
                    unit=unit,
                    source=SOURCE_NAME,
                    series_id=series_id,
                    source_url=url,
                    method="월말 기준값 (원천: 일별)",
                )
            )
    return out


def _collect_fx_usd(
    countries: list, indicators: list, ctx: CollectContext, start: str
) -> list[Observation]:
    """WS_XRU `M.{bis}.{ccy}.E` → 월별 기말 대미환율(현지통화/USD).

    US는 정의상 1.0이므로 수집하지 않는다(프론트/API가 기준통화로 처리).
    """
    indicator = find_indicator(indicators, "fx_usd")
    unit = indicator_unit(indicator, "lcu_per_usd")
    targets = _bis_targets(countries, indicator)
    targets.pop("US", None)
    if not targets:
        return []

    areas = "+".join(dict.fromkeys(targets.values()))
    currencies: dict[str, str] = {}
    for country in _matched(countries, targets):
        ccy = _currency(country)
        if ccy:
            currencies[str(field_of(country, "iso")).upper()] = ccy
    if not currencies:
        ctx.log(f"[{SOURCE_NAME}] WS_XRU: 통화 코드(country.ccy)가 없어 건너뜀")
        return []

    # REF_AREA × CURRENCY 교차 요청이지만 SDMX는 존재하는 조합만 돌려준다.
    key = f"M.{areas}.{'+'.join(dict.fromkeys(currencies.values()))}.E"
    rows = _fetch("WS_XRU", key, start, ctx, list(targets.values()))
    if rows is None:
        return []

    out: list[Observation] = []
    for iso, bis_code in _by_iso(targets):
        ccy = currencies.get(iso)
        if not ccy:
            continue
        pairs = _pairs(rows, bis_code, currency=ccy, freq="M", ctx=ctx, iso=iso, flow="WS_XRU")
        if not pairs:
            ctx.log(f"[{SOURCE_NAME}] {iso}: WS_XRU({bis_code}/{ccy}) 데이터 없음")
            continue
        for period, value in pairs:
            out.append(
                Observation(
                    indicator="fx_usd",
                    iso=iso,
                    freq="M",
                    period=period,
                    value=value,
                    unit=unit,
                    source=SOURCE_NAME,
                    series_id=f"WS_XRU/1.0/M.{bis_code}.{ccy}.E",
                    source_url=PORTAL_URL["WS_XRU"],
                    method="월말 기준 대미환율 (BIS WS_XRU, COLLECTION=E 기말)",
                )
            )
    return out


def _collect_house_price(
    countries: list, indicators: list, ctx: CollectContext, start: str
) -> list[Observation]:
    """WS_SPP `Q.{bis}.N.628` → 분기 명목 주택가격지수(2010=100)."""
    indicator = find_indicator(indicators, "house_price_index")
    unit = indicator_unit(indicator, "index")
    targets = _bis_targets(countries, indicator)
    if not targets:
        return []

    key = "Q." + "+".join(dict.fromkeys(targets.values())) + ".N.628"
    rows = _fetch("WS_SPP", key, start, ctx, list(targets.values()))
    if rows is None:
        return []

    out: list[Observation] = []
    for iso, bis_code in _by_iso(targets):
        pairs = _pairs(rows, bis_code, freq="Q", ctx=ctx, iso=iso, flow="WS_SPP")
        if not pairs:
            # AR·SA는 BIS 주택가격 통계에 없다. 오류가 아니라 커버리지 공백.
            ctx.log(f"[{SOURCE_NAME}] {iso}: WS_SPP 데이터 없음 (BIS 미수록)")
            continue
        for period, value in pairs:
            out.append(
                Observation(
                    indicator="house_price_index",
                    iso=iso,
                    freq="Q",
                    period=period,
                    value=value,
                    unit=unit,
                    source=SOURCE_NAME,
                    series_id=f"WS_SPP/1.0/Q.{bis_code}.N.628",
                    source_url=PORTAL_URL["WS_SPP"],
                    method="분기 명목 주택가격지수 2010=100 (BIS WS_SPP)",
                )
            )
    return out


# ====================================================================== 공통
def _start_period(since: date | None, freq: str) -> str:
    """startPeriod를 빈도별 룩백만큼 뒤로 밀어 준다.

    BIS는 조회 구간에 관측이 하나도 없으면 데이터가 아니라 404(No results)를
    준다. 게다가 발표 지연이 커서(2026-09-20 실측: WS_SPP 최신 분기가 2026-Q1,
    WS_CBPOL은 국가별로 최대 2개월 지연) `since`를 그대로 넣으면 전체 요청이
    404가 된다. 저장은 덮어쓰기(upsert)이므로 구간을 겹치게 잡아 개정값까지
    받는 편이 안전하다.
      D → 7일  ·  M → 3개월  ·  Q → 4분기
    """
    if since is None:
        return DEFAULT_START
    if freq == "M":
        year, month = since.year, since.month - LOOKBACK_MONTHS
        while month < 1:
            year, month = year - 1, month + 12
        return date(year, month, 1).strftime("%Y-%m-%d")
    if freq == "Q":
        quarter_start = ((since.month - 1) // 3) * 3 + 1
        year, month = since.year, quarter_start - 3 * LOOKBACK_QUARTERS
        while month < 1:
            year, month = year - 1, month + 12
        return date(year, month, 1).strftime("%Y-%m-%d")
    return (since - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")


def _bis_targets(countries: list, indicator: Any) -> dict[str, str]:
    """{iso: bis_code} 매핑. 유로 참조 국가와 only 제약은 iter_countries가 걸러낸다."""
    targets: dict[str, str] = {}
    for country in iter_countries(countries, indicator, SOURCE_NAME):
        iso = str(field_of(country, "iso") or "").upper()
        code = country_code(country, "bis") or ("XM" if iso == "EU" else iso)
        targets[iso] = str(code).upper()
    return targets


def _matched(countries: list, targets: dict[str, str]) -> list:
    return [c for c in countries if str(field_of(c, "iso") or "").upper() in targets]


def _currency(country: Any) -> str | None:
    """대미환율 조회용 통화 코드. registry의 ccy 또는 codes["bis_ccy"]."""
    ccy = country_code(country, "bis_ccy") or field_of(country, "ccy")
    return str(ccy).upper() if ccy else None


def _by_iso(targets: dict[str, str]) -> list[tuple[str, str]]:
    return sorted(targets.items())


def _fetch(
    flow: str, key: str, start: str, ctx: CollectContext, isos: list[str]
) -> list[dict[str, str]] | None:
    """SDMX-CSV를 받아 행 리스트로 준다. 실패면 record_error 후 None."""
    url = f"{API_BASE}/{flow}/1.0/{key}"
    params = {"format": "csv", "startPeriod": start}
    try:
        resp = ctx.get(url, params=params)
    except Exception as exc:  # noqa: BLE001 - 네트워크 실패는 격리하고 다음 지표로
        ctx.record_error(SOURCE_NAME, ",".join(sorted(set(isos))), f"{flow} 요청 실패: {exc}")
        return None
    if resp.status_code != 200:
        label = ",".join(sorted(set(isos)))
        if resp.status_code == 404 and _is_no_results(resp.text):
            # BIS는 "조회 구간에 관측 없음"도 404로 준다. 커버리지 공백이지 실패가 아니다.
            ctx.log(f"[{SOURCE_NAME}] {flow} {key}: 조회 구간에 데이터 없음 ({label})")
            return []
        ctx.record_error(SOURCE_NAME, label, f"{flow} HTTP {resp.status_code}")
        return None
    text = resp.text
    ctx.save_raw(SOURCE_NAME, f"{flow}_{key}", text)
    rows = sdmx_csv_to_rows(text)
    if not rows:
        ctx.record_error(
            SOURCE_NAME, ",".join(sorted(set(isos))), f"{flow} 응답이 비었거나 CSV가 아님"
        )
        return None
    return rows


def _is_no_results(text: str) -> bool:
    """SDMX 오류 본문이 "No results for query"(code 100)인지 본다."""
    body = (text or "")[:1000]
    return 'code="100"' in body or "No results" in body


def _pairs(
    rows: list[dict[str, str]],
    ref_area: str,
    currency: str | None = None,
    *,
    freq: str | None = None,
    ctx: CollectContext | None = None,
    iso: str | None = None,
    flow: str = "",
) -> list[tuple[str, float]]:
    """응답 행에서 한 국가의 (TIME_PERIOD, OBS_VALUE)를 기간 오름차순으로 뽑는다.

    `freq`를 주면 TIME_PERIOD 형식(D `YYYY-MM-DD` · M `YYYY-MM` · Q `YYYY-Qn`)을 검증하고,
    어긋나는 행은 건너뛴 뒤 국가 단위로 `ctx.record_error` 1건을 남긴다(다른 행·국가는 계속).
    """
    pattern = _PERIOD_RE.get(freq or "")
    out: list[tuple[str, float]] = []
    bad: list[str] = []
    for row in rows:
        if (row.get("REF_AREA") or "").upper() != ref_area:
            continue
        if currency and (row.get("CURRENCY") or "").upper() != currency:
            continue
        period = (row.get("TIME_PERIOD") or "").strip()
        value = safe_float(row.get("OBS_VALUE"))
        if not period or value is None:
            continue
        if pattern is not None and not pattern.match(period):
            bad.append(period)
            continue
        out.append((period, value))
    if bad and ctx is not None:
        sample = ", ".join(repr(b) for b in bad[:3]) + (" …" if len(bad) > 3 else "")
        ctx.record_error(
            SOURCE_NAME,
            iso or ref_area,
            f"{flow or 'BIS'} TIME_PERIOD 형식 오류 {len(bad)}행 건너뜀 "
            f"(기대 {_PERIOD_HINT.get(freq or '', freq)}): {sample}",
        )
    out.sort(key=lambda p: p[0])
    return out
