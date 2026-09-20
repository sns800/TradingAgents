# ============================================================
# [모듈 개요] World Bank WITS 수출 구성 소스 (exports_top_hs2, 복합값 payload)
#
# WITS(World Integrated Trade Solution) 무료 API에서 국가별 최신 연도 수출액을
# 품목군별로 받아 상위 10개 비중을 CONTRACT.md 3장 복합값
# (`value=None`, `payload={"items":[...], "total_usd_mn":...}`)으로 저장합니다.
# 인증·키 없음. 연간 데이터지만 신규 연도 감지를 위해 월 1회 돕니다.
#
# [실측으로 확정한 것] (2026-09-20)
#   성공 URL 예:
#     https://wits.worldbank.org/API/V1/SDMX/V21/datasource/tradestats-trade/
#       reporter/kor/year/2023/partner/wld/product/all/indicator/XPRT-TRD-VL
#     → HTTP 200, application/vnd.sdmx.structurespecificdata+xml;version=2.1
#     응답 구조: <Series FREQ="A" REPORTER="KOR" PARTNER="WLD"
#                        PRODUCTCODE="84-85_MachElec" INDICATOR="XPRT-TRD-VL">
#                  <Obs TIME_PERIOD="2023" OBS_VALUE="..." DATASOURCE="WITS-CMT"/>
#                </Series>   (요청 1건 = 29개 Series)
#   OBS_VALUE 단위는 **US$ 천(thousand)** → 1000으로 나눠 US$ 백만으로 저장.
#   최신 연도: **2023**. year/2024는 HTTP 404(NoRecordsFound) — 실측.
#   리포터: 266개국 (G20 19개국 전부 존재). 유로존/EU 집계 코드는 없음 → 건너뜀.
#   지표 코드: XPRT-TRD-VL(무역액) · XPRT-PRDCT-SHR(품목 비중 %) 등 8종.
#     비중은 XPRT-PRDCT-SHR로도 받을 수 있지만, payload에 금액(usd_mn)이
#     필요하므로 요청 1건(XPRT-TRD-VL)만 하고 비중은 합계로 계산합니다.
#     검증: KOR 2023 16개 품목군 합 = 631,804,230.615 천달러 =
#           `Total` 시리즈 값과 **정확히 일치**(= 6,318억 달러).
#
# [주의 — HS2 챕터가 아니라 "품목군"입니다] tradestats-trade의 product 차원은
# HS 챕터 97개가 아니라 **31개 그룹**만 지원합니다(`product/85` 요청은
# `Invalid Product Code`). 메타 조회
#   https://wits.worldbank.org/API/V1/wits/datasource/tradestats-trade/product/all
# 로 확인한 그룹 유형: Sector(HS 챕터 구간 16개 + Total), SITC-Rev2-Groups,
# Stages-Of-Processing. 이 모듈은 HS 챕터 구간 16개(`01-05_Animal` …
# `90-99_Miscellan`)만 씁니다. 따라서 payload의 `hs2` 값은 2자리 챕터가 아니라
# **챕터 구간 문자열**("84-85" 등)입니다 — 프론트/계약 주석 반영 필요(보고서).
# 챕터 구간이 한 챕터뿐이면(27-27) HS2_LABELS_KO의 챕터 한글명을 씁니다.
# 향후 HS2 상세 소스(UN Comtrade 등)를 붙이면 `hs2_label_ko()`를 그대로 씁니다.
#
# 국가별 실패 격리: 국가마다 요청 1건이며, 실패는 ctx.record_error(...)로 기록만
# 하고 다음 국가로 넘어갑니다.
# ============================================================
from __future__ import annotations

import re
from datetime import date
from typing import Any

from macro.schema import Observation
from macro.sources.base import (
    CollectContext,
    country_code,
    field_of,
    find_indicator,
    iter_countries,
    safe_float,
)

SOURCE_NAME = "wits"
CADENCE = "monthly"

API_BASE = "https://wits.worldbank.org/API/V1/SDMX/V21/datasource/tradestats-trade"
DOCS_URL = "https://wits.worldbank.org/witsapiintro.aspx"
INDICATOR_CODE = "XPRT-TRD-VL"

# 최신 연도 탐색: (올해 − 2)부터 이 개수만큼 거꾸로 시도한다. WITS는 통상
# 2년 지연이며 2026-09 기준 최신은 2023.
LATEST_YEAR_LAG = 2
LATEST_YEAR_SEARCH_BACK = 5

# 상위 몇 개 품목군을 payload에 담을지
TOP_N = 10

# 연도 탐색 결과를 국가 간에 재사용하는 ctx.extra 키
EXTRA_YEAR_KEY = "wits_year"

METHOD = (
    "WITS tradestats-trade 수출액(XPRT-TRD-VL, US$ 천)을 HS 챕터 구간 16개 품목군으로 "
    "받아 합계 대비 비중을 계산하고 상위 {top}개를 담음. 금액은 US$ 백만 단위"
)

SUPPORTED: tuple[str, ...] = ("exports_top_hs2",)

# HS 챕터 구간 품목군 (WITS grouptype="Sector", Total 제외) → 한글·영문 라벨
WITS_SECTOR_LABELS: dict[str, tuple[str, str]] = {
    "01-05_Animal": ("동물성 제품", "Animal"),
    "06-15_Vegetable": ("식물성 제품", "Vegetable"),
    "16-24_FoodProd": ("가공식품", "Food Products"),
    "25-26_Minerals": ("광물", "Minerals"),
    "27-27_Fuels": ("광물성 연료·에너지", "Fuels"),
    "28-38_Chemicals": ("화학제품", "Chemicals"),
    "39-40_PlastiRub": ("플라스틱·고무", "Plastic or Rubber"),
    "41-43_HidesSkin": ("원피·가죽", "Hides and Skins"),
    "44-49_Wood": ("목재·펄프·종이", "Wood"),
    "50-63_TextCloth": ("섬유·의류", "Textiles and Clothing"),
    "64-67_Footwear": ("신발·모자", "Footwear"),
    "68-71_StoneGlas": ("석재·유리·귀금속", "Stone and Glass"),
    "72-83_Metals": ("금속", "Metals"),
    "84-85_MachElec": ("기계·전기기기", "Mach and Elec"),
    "86-89_Transport": ("운송장비", "Transportation"),
    "90-99_Miscellan": ("기타(광학·정밀기기 등)", "Miscellaneous"),
}

# HS2 챕터(2자리) 한글명. 빈출 챕터를 한글로 두고, 표에 없는 챕터는 호출자가
# 영문 원문을 그대로 쓴다(fail-open). 향후 HS2 상세 소스에서 재사용한다.
HS2_LABELS_KO: dict[str, str] = {
    "01": "산 동물",
    "02": "육류",
    "03": "어패류",
    "04": "낙농품·달걀",
    "07": "채소",
    "08": "과실·견과",
    "09": "커피·차·향신료",
    "10": "곡물",
    "12": "채유용 종자",
    "15": "동식물성 유지",
    "16": "육·어류 조제품",
    "17": "당류·설탕",
    "19": "곡물 조제품",
    "20": "채소·과실 조제품",
    "21": "기타 조제식료품",
    "22": "음료·주류",
    "23": "사료",
    "24": "담배",
    "25": "소금·황·토석",
    "26": "광·슬래그",
    "27": "광물성 연료·에너지",
    "28": "무기화학품",
    "29": "유기화학품",
    "30": "의약품",
    "31": "비료",
    "32": "염료·안료",
    "33": "향료·화장품",
    "38": "각종 화학공업 생산품",
    "39": "플라스틱",
    "40": "고무",
    "41": "원피·가죽",
    "44": "목재",
    "47": "펄프",
    "48": "지류",
    "52": "면",
    "61": "편물 의류",
    "62": "직물 의류",
    "64": "신발",
    "70": "유리",
    "71": "귀금속·보석",
    "72": "철강",
    "73": "철강 제품",
    "74": "구리",
    "76": "알루미늄",
    "84": "기계류",
    "85": "전기기기·전자",
    "87": "자동차",
    "88": "항공기",
    "89": "선박",
    "90": "광학·의료기기",
    "94": "가구",
    "99": "특수거래 품목",
}

# `<Series ...>` 바로 뒤에 오는 `<Obs .../>`를 한 번에 잡는다 (네임스페이스 접두사 허용).
_SERIES_OBS_RE = re.compile(
    r"<(?:\w+:)?Series\b[^>]*?PRODUCTCODE=\"(?P<product>[^\"]+)\"[^>]*?>\s*"
    r"<(?:\w+:)?Obs\b[^>]*?TIME_PERIOD=\"(?P<period>\d{4})\"[^>]*?"
    r"OBS_VALUE=\"(?P<value>[^\"]*)\"",
    re.S,
)
_SECTOR_RE = re.compile(r"^(\d{2})-(\d{2})_")


# ====================================================================== 공개 함수
def hs2_label_ko(code: str) -> str | None:
    """HS2 2자리 챕터 코드의 한글명 (표에 없으면 None → 영문 원문 사용)."""
    return HS2_LABELS_KO.get(str(code or "").strip().zfill(2))


def sector_labels(product_code: str) -> tuple[str, str, str] | None:
    """WITS 품목군 코드 → `(hs 챕터 구간, 한글 라벨, 영문 라벨)`.

    HS 챕터 구간 그룹이 아니면(SITC·가공단계 그룹, Total) None.
    """
    m = _SECTOR_RE.match(product_code or "")
    if not m:
        return None
    low, high = m.group(1), m.group(2)
    span = low if low == high else f"{low}-{high}"
    ko, en = WITS_SECTOR_LABELS.get(product_code, ("", product_code))
    if not ko:
        ko = hs2_label_ko(low) or en
    elif low == high:
        ko = hs2_label_ko(low) or ko
    return span, ko, en


def data_url(reporter_iso3: str, year: int) -> str:
    """브라우저에서도 열리는 WITS SDMX 요청 URL (관측치 상세의 source_url)."""
    return (
        f"{API_BASE}/reporter/{reporter_iso3.lower()}/year/{year}"
        f"/partner/wld/product/all/indicator/{INDICATOR_CODE}"
    )


def parse_exports(xml_text: str) -> dict[str, float]:
    """WITS 구조특화 XML → `{품목군 코드: 수출액(US$ 천)}`."""
    out: dict[str, float] = {}
    for m in _SERIES_OBS_RE.finditer(xml_text or ""):
        value = safe_float(m.group("value"))
        if value is None:
            continue
        out[m.group("product")] = value
    return out


def build_payload(raw: dict[str, float], top_n: int = TOP_N) -> dict[str, Any] | None:
    """`{품목군: 수출액(US$ 천)}` → exports_top_hs2 payload (상위 top_n)."""
    sectors: list[tuple[str, str, str, float]] = []
    for product_code, value in raw.items():
        labels = sector_labels(product_code)
        if labels is None or value <= 0:
            continue
        span, ko, en = labels
        sectors.append((span, ko, en, value))
    if not sectors:
        return None

    total = sum(v for *_, v in sectors)
    if total <= 0:
        return None
    sectors.sort(key=lambda row: row[3], reverse=True)
    items = [
        {
            # label/value는 복합값 3종이 공유하는 공통 쌍 (프론트 렌더 규칙 통일)
            "label": ko,
            "value": round(value / total * 100.0, 4),
            "hs2": span,
            "label_ko": ko,
            "label_en": en,
            "usd_mn": round(value / 1000.0, 3),
        }
        for span, ko, en, value in sectors[:top_n]
    ]
    return {
        "items": items,
        "total_usd_mn": round(total / 1000.0, 3),
        "n_groups": len(sectors),
    }


def collect(
    countries: list[Any],
    indicators: list[Any],
    ctx: CollectContext,
) -> list[Observation]:
    """국가별 최신 연도 수출 품목군 상위 10개를 수집한다 (CONTRACT 7장).

    응답이 크므로 국가마다 최신 1개 연도만 요청한다. 최신 연도는 첫 국가에서
    한 번 탐색해 ctx.extra에 캐시하고 나머지 국가가 재사용한다.
    """
    if not _is_wanted(indicators, "exports_top_hs2"):
        return []
    ind = find_indicator(indicators, "exports_top_hs2") or _FallbackIndicator("exports_top_hs2")

    obs: list[Observation] = []
    skipped: list[str] = []
    for country in iter_countries(countries, ind, SOURCE_NAME):
        iso = str(field_of(country, "iso") or "").upper()
        iso3 = _reporter_code(country)
        if not iso or not iso3:
            skipped.append(iso or "?")
            continue
        try:
            ob = _country_obs(ctx, iso, iso3)
        except Exception as exc:  # noqa: BLE001 - 국가별 실패 격리
            ctx.record_error(SOURCE_NAME, iso, f"수출 품목군 수집 실패: {exc}")
            continue
        if ob is not None:
            obs.append(ob)
    if skipped:
        ctx.log(
            f"[{SOURCE_NAME}] 리포터 코드 없음으로 건너뜀: {','.join(sorted(set(skipped)))} "
            "(WITS에 유로존 집계 리포터가 없음)"
        )
    _log_coverage(ctx, countries, ind, obs)
    return obs


# ====================================================================== 내부 구현
def _reporter_code(country: Any) -> str:
    """WITS 리포터 코드(ISO3). registry에 wits 코드가 없으면 wb/iso3를 쓴다."""
    for key in ("wits", "wb"):
        code = country_code(country, key)
        if isinstance(code, dict):
            code = code.get("iso3") or code.get("code")
        code = str(code or "").upper()
        if len(code) == 3 and code.isalpha():
            return code
    iso3 = str(field_of(country, "iso3") or "").upper()
    return iso3 if len(iso3) == 3 and iso3.isalpha() else ""


def _candidate_years(ctx: CollectContext) -> list[int]:
    """시도할 연도 목록. 이미 확정된 연도가 있으면 그것만."""
    cached = ctx.extra.get(EXTRA_YEAR_KEY)
    if isinstance(cached, int):
        return [cached]
    newest = date.today().year - LATEST_YEAR_LAG
    if ctx.since is not None:
        newest = max(newest, ctx.since.year)
    return [newest - i for i in range(LATEST_YEAR_SEARCH_BACK)]


def _country_obs(ctx: CollectContext, iso: str, iso3: str) -> Observation | None:
    """한 국가의 최신 연도 exports_top_hs2 Observation (없으면 None)."""
    for year in _candidate_years(ctx):
        url = data_url(iso3, year)
        resp = ctx.get(url)
        if resp.status_code == 404:
            # WITS는 "해당 연도 데이터 없음"을 404로 돌려준다 → 이전 연도 시도.
            continue
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}")
        raw = parse_exports(resp.text)
        payload = build_payload(raw)
        if payload is None:
            continue
        ctx.extra[EXTRA_YEAR_KEY] = year
        ctx.save_raw(SOURCE_NAME, f"exports_{iso3}_{year}", resp.text)
        return Observation(
            indicator="exports_top_hs2",
            iso=iso,
            freq="Y",
            period=f"{year:04d}",
            value=None,
            payload=payload,
            unit="pct_share",
            source=SOURCE_NAME,
            series_id=f"tradestats-trade/{iso3}/wld/{INDICATOR_CODE}",
            source_url=url,
            method=METHOD.format(top=TOP_N),
        )
    ctx.log(f"[{SOURCE_NAME}] {iso}({iso3}) 최근 {LATEST_YEAR_SEARCH_BACK}개 연도에 데이터 없음")
    return None


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
    countries: list[Any],
    ind: Any,
    obs: list[Observation],
) -> None:
    """값을 못 받은 국가를 한 줄로 알린다."""
    target = {
        str(field_of(c, "iso") or "").upper() for c in iter_countries(countries, ind, SOURCE_NAME)
    } - {""}
    missing = sorted(target - {ob.iso for ob in obs})
    if missing:
        ctx.log(f"[{SOURCE_NAME}] exports_top_hs2 미수집 {len(missing)}개국: {','.join(missing)}")
