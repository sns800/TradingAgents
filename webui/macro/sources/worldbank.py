# ============================================================
# [모듈 개요] World Bank Indicators API 정량 소스 — 연간 구조·재정·군사·무역 지표
#
# 엔드포인트:
#   https://api.worldbank.org/v2/country/{ISO3;ISO3;...}/indicator/{CODE}
#       ?format=json&per_page=2000&date=1990:2026
# 국가를 `;`로 묶을 수 있어 지표당 1회(+페이지네이션) 호출로 20개국을 받는다.
# 유로존 ISO3는 `EMU` (응답의 `country.id`는 "XC"이므로 매칭은 반드시
# `countryiso3code`로 한다 — 2026-09-20 실측).
#
# 응답 형태: `[ {page, pages, per_page, total, sourceid, lastupdated},
#               [ {indicator:{id,value}, country:{id,value}, countryiso3code,
#                  date:"2024", value: 1.87e12, unit, obs_status, decimal}, ... ] ]`
# `lastupdated`(예 "2026-07-13")를 Observation.vintage로 쓴다.
#
# 모든 관측은 연간(freq Y, period "YYYY")이고 value가 null인 해는 건너뛴다.
# 지표→WB 코드는 registry의 `indicator.source_entries("worldbank")`에서 읽고,
# 비어 있으면 아래 DEFAULT_CODES(제안서 부록 A)를 쓴다.
#
# [2026-09-20 커버리지 실측] 20개국 중 값이 있는 국가 수 / 최신 연도
#   gdp_usd·gdp_growth·gni_usd·gni_pc·va_*·exports_gdp  20/20, 2025
#   mil_gdp·mil_usd 20/20 · mil_expenditure_share 19/20(TR 없음), 2024
#   gov_expense_gdp 18/20(CN·ID 없음) · gov_revenue_gdp 18/20(ID·JP 없음)
#   gov_debt_gdp 11/20 (AR·CN·DE·EMU·FR·ID·IT·JP·SA 없음 → IMF FM 폴백 필요)
#   energy_import_dep 20/20, 2023
# ============================================================
from __future__ import annotations

import json
from datetime import date
from typing import Any

from macro.schema import Observation
from macro.sources.base import (
    CollectContext,
    country_code,
    field_of,
    indicator_unit,
    iter_countries,
    safe_float,
    source_entries,
)

SOURCE_NAME = "worldbank"
CADENCE = "monthly"

API_BASE = "https://api.worldbank.org/v2"
PER_PAGE = 2000
DEFAULT_DATE_RANGE = "1990:2026"
MAX_PAGES = 10  # 안전 상한 (20개국 × 37년 ≈ 740행이면 1페이지)
LOOKBACK_YEARS = 5  # 증분 수집 룩백 (연간 지표의 발표 지연·개정 흡수)

# 지표 id → (WB 코드, 기본 unit). CONTRACT 2장 + 제안서 부록 A.
DEFAULT_CODES: dict[str, tuple[str, str]] = {
    "gdp_usd": ("NY.GDP.MKTP.CD", "usd"),
    "gdp_growth": ("NY.GDP.MKTP.KD.ZG", "%"),
    "gni_usd": ("NY.GNP.MKTP.CD", "usd"),
    "gni_pc": ("NY.GNP.PCAP.CD", "usd"),
    "gov_expense_gdp": ("GC.XPN.TOTL.GD.ZS", "pct_gdp"),
    "gov_revenue_gdp": ("GC.REV.XGRT.GD.ZS", "pct_gdp"),
    "gov_debt_gdp": ("GC.DOD.TOTL.GD.ZS", "pct_gdp"),
    "mil_gdp": ("MS.MIL.XPND.GD.ZS", "pct_gdp"),
    "mil_expenditure_share": ("MS.MIL.XPND.ZS", "pct_share"),
    "mil_usd": ("MS.MIL.XPND.CD", "usd"),
    "va_agri": ("NV.AGR.TOTL.ZS", "pct_gdp"),
    "va_industry": ("NV.IND.TOTL.ZS", "pct_gdp"),
    "va_manuf": ("NV.IND.MANF.ZS", "pct_gdp"),
    "va_services": ("NV.SRV.TOTL.ZS", "pct_gdp"),
    "exports_gdp": ("NE.EXP.GNFS.ZS", "pct_gdp"),
    "energy_import_dep": ("EG.IMP.CONS.ZS", "pct_share"),
}

# ISO2 → ISO3 최소 폴백 (registry가 codes["wb"]/iso3를 주면 그쪽이 우선)
ISO3_FALLBACK = {
    "KR": "KOR", "US": "USA", "JP": "JPN", "CN": "CHN", "EU": "EMU",
    "DE": "DEU", "FR": "FRA", "IT": "ITA", "GB": "GBR", "CA": "CAN",
    "AU": "AUS", "IN": "IND", "ID": "IDN", "BR": "BRA", "MX": "MEX",
    "AR": "ARG", "TR": "TUR", "SA": "SAU", "ZA": "ZAF", "RU": "RUS",
}


def collect(countries: list, indicators: list, ctx: CollectContext) -> list[Observation]:
    """World Bank Indicators API에서 연간 관측치를 수집한다.

    지표별 실패 격리: 한 지표의 요청/파싱이 실패하면 대상 국가 전체에
    record_error를 남기고 다음 지표로 넘어간다. 응답에 없는 국가(gov_debt_gdp의
    일본 등)는 오류가 아니라 커버리지 공백이므로 로그만 남긴다.
    """
    out: list[Observation] = []
    date_range = _date_range(ctx)

    for indicator in indicators or []:
        indicator_id = field_of(indicator, "id")
        if not indicator_id:
            continue
        spec = _wb_code(indicator, indicator_id)
        if not spec:
            continue
        code, entry_unit = spec
        # 소스 항목이 unit을 덮어쓸 수 있다 (예: m2_level의 WB 폴백은 잔액이
        # 아니라 광의통화/GDP라서 usd_bn이 아니라 pct_gdp).
        unit = entry_unit or indicator_unit(
            indicator, DEFAULT_CODES.get(indicator_id, ("", "pct_gdp"))[1]
        )

        targets: dict[str, str] = {}
        for country in iter_countries(countries, indicator, SOURCE_NAME):
            iso = str(field_of(country, "iso") or "").upper()
            iso3 = _iso3(country, iso)
            if iso3:
                targets[iso3] = iso
        if not targets:
            continue

        payload = _fetch(code, list(targets), date_range, ctx)
        if payload is None:
            continue
        rows, last_updated = payload
        seen: set[str] = set()
        for row in rows:
            iso3 = (row.get("countryiso3code") or "").upper()
            iso = targets.get(iso3)
            if not iso:
                continue
            period = str(row.get("date") or "").strip()
            value = safe_float(row.get("value"))
            if value is None or len(period) != 4 or not period.isdigit():
                continue
            seen.add(iso)
            out.append(
                Observation(
                    indicator=indicator_id,
                    iso=iso,
                    freq="Y",
                    period=period,
                    value=value,
                    unit=unit,
                    source=SOURCE_NAME,
                    series_id=code,
                    source_url=(
                        f"https://data.worldbank.org/indicator/{code}?locations={iso3}"
                    ),
                    method="세계은행 연간 원천값 (World Bank Indicators API)",
                    **({"vintage": last_updated} if last_updated else {}),
                )
            )
        missing = sorted(set(targets.values()) - seen)
        if missing:
            ctx.log(f"[{SOURCE_NAME}] {indicator_id}({code}) 값 없는 국가: {','.join(missing)}")
    return out


# ====================================================================== 내부
def _date_range(ctx: CollectContext) -> str:
    """조회 연도 범위를 만든다. since가 있으면 5년 룩백을 둔다.

    ※ WB는 `date` 범위에 맞는 연도가 하나도 없으면 필터를 조용히 무시하고 전
    기간(1960~)을 돌려준다 — 2026-09-20 실측(`date=2026:2028` → total 66년치).
    연간 지표는 발표가 1~3년 늦으므로(energy_import_dep 최신 2023, mil_* 2024)
    since를 그대로 쓰면 필터가 무력화된다. 5년 룩백으로 항상 실재 연도를
    포함시키고, 개정값도 함께 받는다(저장은 덮어쓰기).
    """
    if ctx.since is None:
        return DEFAULT_DATE_RANGE
    start = max(1960, ctx.since.year - LOOKBACK_YEARS)
    end = max(ctx.since.year + 1, date.today().year + 1)
    return f"{start}:{end}"


def _wb_code(indicator: Any, indicator_id: str) -> tuple[str, str | None] | None:
    """registry의 소스 항목에서 (WB 코드, unit 오버라이드)를 읽는다.

    항목이 없으면 DEFAULT_CODES(제안서 부록 A)를 쓴다. `{...}` 치환 템플릿은
    이 모듈이 풀 수 없으므로 건너뛴다.
    """
    for entry in source_entries(indicator, SOURCE_NAME):
        series = str(entry.get("series") or "")
        if not series or "{" in series:
            continue
        unit = entry.get("unit")
        return series, (str(unit) if unit else None)
    default = DEFAULT_CODES.get(indicator_id)
    return (default[0], None) if default else None


def _iso3(country: Any, iso: str) -> str | None:
    code = country_code(country, "wb") or field_of(country, "iso3")
    if code:
        return str(code).upper()
    return ISO3_FALLBACK.get(iso)


def _fetch(
    code: str, iso3s: list[str], date_range: str, ctx: CollectContext
) -> tuple[list[dict[str, Any]], str | None] | None:
    """모든 페이지를 모아 (행 리스트, lastupdated)를 준다. 실패면 None."""
    joined = ";".join(sorted(iso3s))
    url = f"{API_BASE}/country/{joined}/indicator/{code}"
    isos_label = ",".join(sorted(iso3s))
    rows: list[dict[str, Any]] = []
    last_updated: str | None = None
    page = 1
    while page <= MAX_PAGES:
        params = {
            "format": "json",
            "per_page": PER_PAGE,
            "date": date_range,
            "page": page,
        }
        try:
            resp = ctx.get(url, params=params)
        except Exception as exc:  # noqa: BLE001 - 지표별로 격리하고 계속
            ctx.record_error(SOURCE_NAME, isos_label, f"{code} 요청 실패: {exc}")
            return None
        if resp.status_code != 200:
            ctx.record_error(SOURCE_NAME, isos_label, f"{code} HTTP {resp.status_code}")
            return None
        text = resp.text
        if page == 1:
            ctx.save_raw(SOURCE_NAME, f"{code}_{joined.replace(';', '-')}", text)
        try:
            doc = json.loads(text)
        except (ValueError, json.JSONDecodeError) as exc:
            ctx.record_error(SOURCE_NAME, isos_label, f"{code} JSON 파싱 실패: {exc}")
            return None
        if not isinstance(doc, list) or not doc:
            ctx.record_error(SOURCE_NAME, isos_label, f"{code} 예상과 다른 응답 형식")
            return None
        header = doc[0] if isinstance(doc[0], dict) else {}
        if "message" in header:
            # WB는 잘못된 코드에 HTTP 200 + {"message":[{"key":...}]}를 준다.
            ctx.record_error(SOURCE_NAME, isos_label, f"{code} 오류 응답: {header['message']}")
            return None
        last_updated = last_updated or header.get("lastupdated")
        body = doc[1] if len(doc) > 1 and isinstance(doc[1], list) else []
        rows.extend(r for r in body if isinstance(r, dict))
        pages = int(header.get("pages") or 1)
        if page >= pages:
            break
        page += 1
    return rows, last_updated
