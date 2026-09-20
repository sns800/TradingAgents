# ============================================================
# [모듈 개요] IMF 신 데이터 포털(SDMX 3.0) 통화량 소스 (m2_level / m2_yoy)
#
# 2025-11에 구 IMF 데이터 포털이 폐지되어 `dataservices.imf.org/REST/SDMX_JSON`
# 계열은 더 쓸 수 없습니다. 이 모듈은 신 포털(data.imf.org)의 SDMX 3.0 REST API
# 에서 통화금융통계(MFS) 광의통화 잔액을 받아 CONTRACT.md 3장 Observation으로
# 바꿉니다. 인증은 없습니다.
#
# [실측으로 확정한 엔드포인트·코드] (2026-09-20)
#   구조 조회(작동): GET https://api.imf.org/external/sdmx/3.0/structure/dataflow/
#                        IMF.STA/MFS_MA/+?references=all
#     · Accept: application/vnd.sdmx.structure+json;version=2.0.0
#     · `structure/datastructure/IMF.STA/MFS_MA/...`는 204(빈 응답) → 쓰지 않는다.
#     · IMF.STA 데이터플로 191건 중 통화량은 `MFS_MA`
#       ("Monetary and Financial Statistics (MFS), Monetary Aggregates", 10.0.1)
#   DSD_MFS_MA 차원 순서: COUNTRY.INDICATOR.UNIT.FREQUENCY  (+ TIME_PERIOD)
#   코드: INDICATOR `BM_MAI`(Broad Money, CL_MFS_MA_INDICATOR 14개 중),
#         UNIT `XDC`(Domestic currency, CL_UNIT), FREQUENCY `M`,
#         COUNTRY는 ISO3 (CL_MFS_COUNTRY 344개)
#   데이터 조회(작동): GET .../3.0/data/dataflow/IMF.STA/MFS_MA/+/{KEY}
#     · Accept: application/vnd.sdmx.data+csv;version=2.0.0
#     · **키에 와일드카드를 쓸 수 없습니다.** `KOR.BM_MAI..M`처럼 빈 칸을 두면
#       HTTP 200 + 0행이 돌아옵니다. 4개 차원을 모두 채워야 합니다.
#       `+` 합집합은 동작 → 국가 전체를 요청 1건으로 묶습니다.
#     · **startPeriod는 무시됩니다.** 기간 필터는 SDMX 3.0 문법
#       `c[TIME_PERIOD]=ge:YYYY-MM` 를 써야 합니다(실측 확인).
#     · TIME_PERIOD 형식이 `2025-M10` → `2025-10`으로 바꿔 저장합니다.
#     · OBS_VALUE는 현지통화 원단위(스케일 없음). SCALE=6은 표시용 힌트일 뿐이라
#       곱하지 않습니다. 검증: KOR 2025-10 = 4.472e15 KRW = 4,472조원(실제 M2와 일치).
#   성공 URL 예:
#     https://api.imf.org/external/sdmx/3.0/data/dataflow/IMF.STA/MFS_MA/+/
#       KOR.BM_MAI.XDC.M?c[TIME_PERIOD]=ge:2015-01
#
# [확정한 커버리지 — 20개국 중 11개국만] BM_MAI.XDC.M 기준 실측
#   최신 제공: KR(2025-10) JP(2026-02) AU(2026-03) BR(2026-06) ID(2026-05)
#              MX(2026-07) TR(2026-02) ZA(2026-07) AR(2026-01)
#   정지    : CA(2008-12) RU(2021-11)   → STALE_AFTER_MONTHS 초과 시 경고 로그
#   미제공  : US CN EU(U2/G163/G995 모두 없음) DE FR IT GB IN SA
#             (INDICATOR 14종 × UNIT XDC/XDCB/IX/PT × FREQ M/Q/A 전수 조회 0행)
#   → 미제공 국가는 수집기가 FRED(US M2SL)·ECB(EU)·World Bank(연간)·ECOS(KR, 향후)
#     폴백을 씁니다. 유로 회원국(DE/FR/IT)은 base.iter_countries가 애초에 제외합니다.
#
# [unit 표기] 값은 **현지통화 10억 단위**라 CONTRACT 2장 unit 열거에 정확히
# 맞는 항목이 없습니다. `index`로 뭉개지 않고 `lcu_bn`을 씁니다(registry.yaml의
# m2_level.unit은 현재 `usd_bn`이라 수정 제안 대상 — 보고서 참고).
#
# 국가별 실패 격리: 요청은 국가 묶음 1건이므로 실패 시 대상 국가 전부에
# ctx.record_error(...)를 남기고 빈 리스트를 돌려줍니다(예외 금지).
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
    sdmx_csv_to_rows,
)

SOURCE_NAME = "imf"
CADENCE = "weekly"

SDMX3_BASE = "https://api.imf.org/external/sdmx/3.0"
DATAFLOW = "IMF.STA/MFS_MA"
STRUCTURE_URL = f"{SDMX3_BASE}/structure/dataflow/{DATAFLOW}/+?references=all"

# SDMX 3.0은 Accept 헤더로 표현 형식을 고른다 (format= 파라미터 없음).
CSV_ACCEPT = "application/vnd.sdmx.data+csv;version=2.0.0"

# 확정된 시리즈 좌표 (위 [모듈 개요] 참고)
INDICATOR_CODE = "BM_MAI"  # Broad Money
UNIT_CODE = "XDC"  # Domestic currency
FREQ_CODE = "M"

# ctx.since가 없을 때의 기본 조회 시작(년)
DEFAULT_LOOKBACK_YEARS = 15

# 마지막 관측이 이 개월 수보다 오래되면 "정지 시리즈"로 보고 경고를 남긴다.
STALE_AFTER_MONTHS = 18

# 현지통화 10억 단위로 저장한다(CONTRACT 2장 unit 열거 확장 필요 → 보고서).
M2_LEVEL_UNIT = "lcu_bn"
BILLION = 1_000_000_000.0

METHOD_LEVEL = (
    "IMF MFS 광의통화(Broad Money, INDICATOR=BM_MAI) 월말 잔액. "
    "원천은 현지통화 원단위이며 10억 단위로 나눠 저장 (SCALE 열은 표시용 힌트라 미적용)"
)
METHOD_YOY = (
    "IMF MFS 광의통화 잔액의 전년동월대비 변화율 — 12개월 전 값 대비로 수집기가 계산 "
    "(원천이 전년비를 직접 제공하지 않음)"
)

SUPPORTED: tuple[str, ...] = ("m2_level", "m2_yoy")

# `2025-M10` / `2025-10` 두 형식을 모두 받는다.
_PERIOD_RE = re.compile(r"^(\d{4})-M?(\d{1,2})$")


def series_key(imf_country: str) -> str:
    """단일 시리즈 SDMX 키. 와일드카드가 동작하지 않아 4개 차원을 모두 채운다."""
    return f"{imf_country}.{INDICATOR_CODE}.{UNIT_CODE}.{FREQ_CODE}"


def data_url(key: str, start_period: str) -> str:
    """브라우저에서도 열리는 SDMX-CSV 데이터 URL (관측치 상세의 source_url)."""
    return f"{SDMX3_BASE}/data/dataflow/{DATAFLOW}/+/{key}?c[TIME_PERIOD]=ge:{start_period}"


def normalize_period(raw: str) -> str | None:
    """IMF의 `YYYY-Mnn` 기간을 CONTRACT 3장 월 기간 `YYYY-MM`으로 바꾼다."""
    m = _PERIOD_RE.match((raw or "").strip())
    if not m:
        return None
    month = int(m.group(2))
    if not 1 <= month <= 12:
        return None
    return f"{m.group(1)}-{month:02d}"


def collect(
    countries: list[Any],
    indicators: list[Any],
    ctx: CollectContext,
) -> list[Observation]:
    """IMF MFS 광의통화를 국가 묶음 1건으로 수집한다 (CONTRACT 7장).

    m2_level은 원천값을, m2_yoy는 12개월 전 값 대비 변화율을 만든다. 요청이
    실패하면 대상 국가 전부에 오류를 기록하고 빈 리스트를 돌려준다.
    """
    want_level = _is_wanted(indicators, "m2_level")
    want_yoy = _is_wanted(indicators, "m2_yoy")
    skipped = [
        str(getattr(i, "id", "") or "")
        for i in (indicators or [])
        if str(getattr(i, "id", "") or "") not in SUPPORTED
    ]
    if skipped:
        # registry가 미구현 폴백(예: gov_debt_gdp의 IMF Fiscal Monitor)을 넘겨주는 경우
        ctx.log(f"[{SOURCE_NAME}] 지원하지 않는 지표 건너뜀: {', '.join(sorted(set(skipped)))}")
    if not (want_level or want_yoy):
        return []

    targets = _targets(countries, indicators)
    if not targets:
        ctx.log(f"[{SOURCE_NAME}] 대상 국가 없음 — 건너뜀")
        return []

    start_period = _start_period(ctx, want_yoy)
    key = "+".join(sorted(targets)) + f".{INDICATOR_CODE}.{UNIT_CODE}.{FREQ_CODE}"
    url = data_url(key, start_period)
    try:
        resp = ctx.get(url, headers={"Accept": CSV_ACCEPT})
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}")
        text = resp.text
    except Exception as exc:  # noqa: BLE001 - 요청 단위 실패 격리
        for iso in sorted(set(targets.values())):
            ctx.record_error(SOURCE_NAME, iso, f"MFS_MA 요청 실패: {exc}")
        return []

    ctx.save_raw(SOURCE_NAME, f"mfs_ma_broad_money_{start_period}", text)
    series = _parse(text, targets)
    if not series:
        ctx.log(
            f"[{SOURCE_NAME}] MFS_MA 응답에 관측치 없음 — "
            "미제공 국가는 FRED/ECB/World Bank 폴백 필요"
        )

    obs: list[Observation] = []
    for (iso, imf_country), points in sorted(series.items()):
        skey = series_key(imf_country)
        surl = data_url(skey, start_period)
        periods = sorted(points)
        if want_level:
            obs.extend(
                Observation(
                    indicator="m2_level",
                    iso=iso,
                    freq="M",
                    period=period,
                    value=points[period] / BILLION,
                    unit=M2_LEVEL_UNIT,
                    source=SOURCE_NAME,
                    series_id=f"{DATAFLOW}/{skey}",
                    source_url=surl,
                    method=METHOD_LEVEL,
                )
                for period in periods
            )
        if want_yoy:
            obs.extend(_yoy_obs(iso, skey, surl, points))
        _warn_if_stale(ctx, iso, periods[-1] if periods else "")

    _log_coverage(ctx, countries, indicators, obs)
    return obs


# ====================================================================== 내부 구현
def _targets(countries: list[Any], indicators: list[Any]) -> dict[str, str]:
    """`{IMF COUNTRY 코드: iso}`. base.iter_countries가 유로 회원국을 걸러 준다."""
    ind = (
        find_indicator(indicators, "m2_level")
        or find_indicator(indicators, "m2_yoy")
        or _FallbackIndicator("m2_level")
    )
    out: dict[str, str] = {}
    for country in iter_countries(countries, ind, SOURCE_NAME):
        code = country_code(country, "imf")
        iso = str(field_of(country, "iso") or "").upper()
        if code and iso:
            out[str(code).upper()] = iso
    return out


def _start_period(ctx: CollectContext, want_yoy: bool) -> str:
    """조회 시작 월. 전년비를 만들려면 요청 시작보다 12개월 더 받아야 한다."""
    if ctx.since is not None:
        year, month = ctx.since.year, ctx.since.month
        if want_yoy:
            year -= 1
        return f"{year:04d}-{month:02d}"
    return f"{date.today().year - DEFAULT_LOOKBACK_YEARS:04d}-01"


def _parse(text: str, targets: dict[str, str]) -> dict[tuple[str, str], dict[str, float]]:
    """SDMX-CSV → `{(iso, IMF 국가코드): {기간: 값}}`."""
    series: dict[tuple[str, str], dict[str, float]] = {}
    for row in sdmx_csv_to_rows(text):
        imf_country = (row.get("COUNTRY") or "").strip().upper()
        iso = targets.get(imf_country)
        period = normalize_period(row.get("TIME_PERIOD", ""))
        value = safe_float(row.get("OBS_VALUE"))
        if not iso or not period or value is None:
            continue
        if (row.get("INDICATOR") or "").strip() != INDICATOR_CODE:
            continue
        series.setdefault((iso, imf_country), {})[period] = value
    return series


def _yoy_obs(
    iso: str,
    skey: str,
    surl: str,
    points: dict[str, float],
) -> list[Observation]:
    """12개월 전 값 대비 전년비를 만든다 (CONTRACT 6장: 지수 원천에서 계산)."""
    obs: list[Observation] = []
    for period, value in sorted(points.items()):
        year, month = int(period[:4]), int(period[5:7])
        prev = points.get(f"{year - 1:04d}-{month:02d}")
        if prev is None or prev == 0:
            continue
        obs.append(
            Observation(
                indicator="m2_yoy",
                iso=iso,
                freq="M",
                period=period,
                value=(value / prev - 1.0) * 100.0,
                unit="%",
                source=SOURCE_NAME,
                series_id=f"{DATAFLOW}/{skey}",
                source_url=surl,
                method=METHOD_YOY,
                flags=["derived"],
            )
        )
    return obs


def _warn_if_stale(ctx: CollectContext, iso: str, last_period: str) -> None:
    """마지막 관측이 오래된 국가(CA·RU 등)를 로그로 알린다."""
    if len(last_period) != 7:
        return
    today = date.today()
    months = (today.year - int(last_period[:4])) * 12 + (today.month - int(last_period[5:7]))
    if months > STALE_AFTER_MONTHS:
        ctx.log(
            f"[{SOURCE_NAME}] {iso} 광의통화 시리즈 정지 상태 (마지막 {last_period}, "
            f"{months}개월 경과) — 폴백 권장"
        )


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
    indicators: list[Any],
    obs: list[Observation],
) -> None:
    """값을 못 받은 국가를 한 줄로 알린다 (폴백 판단용)."""
    ind = find_indicator(indicators, "m2_level") or _FallbackIndicator("m2_level")
    target_iso = {
        str(field_of(c, "iso") or "").upper() for c in iter_countries(countries, ind, SOURCE_NAME)
    } - {""}
    seen = {ob.iso for ob in obs if ob.indicator == "m2_level"}
    missing = sorted(target_iso - seen)
    if missing:
        ctx.log(
            f"[{SOURCE_NAME}] m2_level 미수집 {len(missing)}개국: {','.join(missing)} — "
            "FRED(US)/ECB(EU)/World Bank(연간) 폴백 필요"
        )
