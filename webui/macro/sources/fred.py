# ============================================================
# [모듈 개요] FRED(St. Louis Fed) 미국 전용 정량 소스 — 다른 소스의 US 폴백
#
# 엔드포인트:
#   https://api.stlouisfed.org/fred/series/observations
#       ?series_id=FEDFUNDS&api_key=...&file_type=json&observation_start=YYYY-MM-DD
# 응답(FRED 공식 문서 형식):
#   {"realtime_start":"2026-09-20","realtime_end":"2026-09-20",
#    "observation_start":"2010-01-01","observation_end":"9999-12-31",
#    "units":"lin","output_type":1,"file_type":"json",
#    "order_by":"observation_date","sort_order":"asc","count":195,
#    "offset":0,"limit":100000,
#    "observations":[{"realtime_start":"2026-09-20","realtime_end":"2026-09-20",
#                     "date":"2026-08-01","value":"3.625"}, ...]}
# 결측은 `"value": "."` 로 오므로 safe_float가 None으로 흘려보낸다.
# `realtime_start`(응답 상위 또는 관측 항목)를 Observation.vintage로 쓴다.
#
# `FRED_API_KEY`가 없으면 경고 한 줄만 남기고 빈 리스트를 돌려준다(CONTRACT 7장).
# 2026-09-20 현재 저장소에 키가 없어 실호출 검증은 못 했고, 잘못된 키로
# `HTTP 400 {"error_code":400,"error_message":"...api_key is not registered..."}`
# 가 오는 것만 확인했다(엔드포인트·파라미터 이름은 유효).
#
# US 전용이며 `fallback_source` 플래그는 붙이지 않는다 — 소스 우선순위 판단은
# collect.py(수집기)의 몫이다.
# ============================================================
from __future__ import annotations

import contextlib
import json
import os
from datetime import date
from typing import Any

from macro.schema import Observation
from macro.sources.base import (
    CollectContext,
    field_of,
    indicator_unit,
    month_key,
    safe_float,
    source_entries,
)

SOURCE_NAME = "fred"
CADENCE = "weekly"

API_URL = "https://api.stlouisfed.org/fred/series/observations"
DEFAULT_START = "2010-01-01"
FRED_ISO = "US"

# 지표 id → (시리즈 ID, freq, 기본 unit, method)
DEFAULT_SERIES: dict[str, tuple[str, str, str, str]] = {
    "policy_rate": ("FEDFUNDS", "M", "%", "연방기금 실효금리 월평균 (FRED FEDFUNDS)"),
    "m2_level": ("M2SL", "M", "usd_bn", "M2 계절조정 통화량 잔액 (FRED M2SL, 10억 달러)"),
    "cpi_index": ("CPIAUCSL", "M", "index", "도시 소비자물가지수 계절조정 (FRED CPIAUCSL)"),
    "ppi_index": ("PPIACO", "M", "index", "전 품목 생산자물가지수 (FRED PPIACO)"),
    "house_price_index": (
        "CSUSHPINSA",
        "M",
        "index",
        "케이스-실러 전국 주택가격지수 비조정 (FRED CSUSHPINSA)",
    ),
    "dxy": ("DTWEXBGS", "D", "index", "광범위 달러지수 일별 (FRED DTWEXBGS)"),
}


def collect(countries: list, indicators: list, ctx: CollectContext) -> list[Observation]:
    """FRED에서 미국 지표를 수집한다. 키가 없으면 경고 1줄 + 빈 리스트.

    지표별 실패 격리: 한 시리즈의 요청/파싱 실패는 record_error로 남기고 다음
    시리즈로 넘어간다.
    """
    api_key = os.environ.get("FRED_API_KEY", "").strip()
    if not api_key:
        ctx.log(f"[{SOURCE_NAME}] FRED_API_KEY가 없어 건너뜁니다 (US 폴백 미수집)")
        return []
    if not _us_requested(countries):
        return []

    start = ctx.since.strftime("%Y-%m-%d") if ctx.since else DEFAULT_START
    out: list[Observation] = []
    for indicator in indicators or []:
        indicator_id = field_of(indicator, "id")
        spec = _series_spec(indicator, indicator_id)
        if not spec:
            continue
        series_id, freq, default_unit, method = spec
        unit = indicator_unit(indicator, default_unit)
        parsed = _fetch(series_id, start, api_key, ctx)
        if parsed is None:
            continue
        rows, vintage = parsed
        for obs_date, value in rows:
            period = _period(freq, obs_date)
            if period is None:
                continue
            out.append(
                Observation(
                    indicator=indicator_id,
                    iso=FRED_ISO,
                    freq=freq,
                    period=period,
                    value=value,
                    unit=unit,
                    source=SOURCE_NAME,
                    series_id=series_id,
                    source_url=f"https://fred.stlouisfed.org/series/{series_id}",
                    method=method,
                    **({"vintage": vintage} if vintage else {}),
                )
            )
    return out


# ====================================================================== 내부
def _us_requested(countries: list) -> bool:
    """요청 국가에 US가 없으면 호출하지 않는다 (FRED는 미국 전용)."""
    if not countries:
        return True
    return any(str(field_of(c, "iso") or "").upper() == FRED_ISO for c in countries)


def _series_spec(indicator: Any, indicator_id: str | None) -> tuple[str, str, str, str] | None:
    """registry의 소스 항목(`only: [US]`)을 우선하고, 없으면 기본 매핑을 쓴다."""
    if not indicator_id:
        return None
    default = DEFAULT_SERIES.get(indicator_id)
    for entry in source_entries(indicator, SOURCE_NAME):
        series = str(entry.get("series") or "")
        # `{fred_fx}` 같은 국가별 치환 템플릿(fx_usd)은 US 전용인 이 모듈의
        # 대상이 아니므로 건너뛴다 (US 환율은 정의상 1.0).
        if not series or "{" in series:
            continue
        only = entry.get("only")
        if only and FRED_ISO not in {str(v).upper() for v in only}:
            return None
        freq = str(entry.get("freq") or (default[1] if default else "M")).upper()
        unit = entry.get("unit") or (default[2] if default else "index")
        method = entry.get("method") or (default[3] if default else f"FRED {series} 원천값")
        return str(series), freq, str(unit), str(method)
    return default


def _period(freq: str, obs_date: str) -> str | None:
    """FRED의 관측 날짜(`YYYY-MM-DD`, 월간은 월초)를 CONTRACT 3장 period로 바꾼다."""
    try:
        if freq == "M":
            return month_key(obs_date)
        if freq == "Q":
            y, m = int(obs_date[:4]), int(obs_date[5:7])
            return f"{y}-Q{(m - 1) // 3 + 1}"
        if freq == "Y":
            return obs_date[:4]
        date.fromisoformat(obs_date)  # D: 형식 검증만
        return obs_date
    except (ValueError, IndexError):
        return None


def _fetch(
    series_id: str, start: str, api_key: str, ctx: CollectContext
) -> tuple[list[tuple[str, float]], str | None] | None:
    """관측치 목록과 vintage(realtime_start)를 얻는다. 실패면 None."""
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "observation_start": start,
    }
    try:
        resp = ctx.get(API_URL, params=params)
    except Exception as exc:  # noqa: BLE001 - 시리즈별로 격리하고 계속
        ctx.record_error(SOURCE_NAME, FRED_ISO, f"{series_id} 요청 실패: {exc}")
        return None
    if resp.status_code != 200:
        detail = ""
        # 오류 본문이 JSON이 아닐 수 있으므로 파싱 실패는 조용히 무시한다.
        with contextlib.suppress(Exception):
            detail = json.loads(resp.text).get("error_message", "")
        ctx.record_error(
            SOURCE_NAME, FRED_ISO, f"{series_id} HTTP {resp.status_code} {detail}".strip()
        )
        return None
    text = resp.text
    # API 키가 쿼리에 들어가므로 원본에는 URL을 남기지 않고 본문만 보존한다.
    ctx.save_raw(SOURCE_NAME, series_id, text)
    try:
        doc = json.loads(text)
    except (ValueError, json.JSONDecodeError) as exc:
        ctx.record_error(SOURCE_NAME, FRED_ISO, f"{series_id} JSON 파싱 실패: {exc}")
        return None
    return parse_observations(doc)


def parse_observations(doc: dict[str, Any]) -> tuple[list[tuple[str, float]], str | None]:
    """FRED 응답 → ([(date, value)], vintage). 테스트가 직접 호출한다."""
    vintage = _clean_vintage(doc.get("realtime_start"))
    rows: list[tuple[str, float]] = []
    for item in doc.get("observations") or []:
        if not isinstance(item, dict):
            continue
        obs_date = str(item.get("date") or "").strip()
        value = safe_float(item.get("value"))
        if not obs_date or value is None:
            continue  # 결측은 "." 로 온다
        rows.append((obs_date, value))
        vintage = _clean_vintage(item.get("realtime_start")) or vintage
    rows.sort(key=lambda p: p[0])
    return rows, vintage


def _clean_vintage(value: Any) -> str | None:
    """`YYYY-MM-DD` 형식만 vintage로 인정한다 (`9999-12-31` 같은 센티넬 제외)."""
    s = str(value or "").strip()
    if len(s) != 10 or s.startswith("9999"):
        return None
    try:
        date.fromisoformat(s)
    except ValueError:
        return None
    return s
