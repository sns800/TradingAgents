# ============================================================
# [모듈 개요] OECD SDMX 물가 소스 (cpi_index/cpi_yoy/core_cpi_yoy/ppi_index/ppi_yoy)
#
# OECD Data Explorer의 공개 SDMX REST API에서 월별 물가 지표를 받아 CONTRACT.md
# 3장 Observation으로 바꿉니다. 인증은 없지만 **요청 속도 제한**이 있어 국가를
# `+`로 묶어 전 지표를 요청 4건으로 처리하고, 요청 사이에 1초를 쉽니다. 429를
# 받으면 30초 뒤 1회만 재시도합니다.
#
# [실측으로 확정한 차원 순서] (2026-09-20, datastructure 조회 + 소량 데이터 요청)
#   datastructure/OECD.SDD.TPS/DSD_PRICES(및 DSD_PRICES_COICOP2018, DSD_G20_PRICES)
#     1 REF_AREA 2 FREQ 3 METHODOLOGY 4 MEASURE 5 UNIT_MEASURE
#     6 EXPENDITURE 7 ADJUSTMENT 8 TRANSFORMATION
#   datastructure/OECD.SDD.STES/DSD_KEI
#     1 REF_AREA 2 FREQ 3 MEASURE 4 UNIT_MEASURE 5 ACTIVITY
#     6 ADJUSTMENT 7 TRANSFORMATION
#   코드: METHODOLOGY N(국가 정의)·HICP(EU 조화지수), MEASURE CPI,
#         UNIT_MEASURE IX(지수)·PA(전년비 %), EXPENDITURE _T(전체)·
#         _TXCP01_NRG(식료품·에너지 제외), ADJUSTMENT N, TRANSFORMATION _Z·GY
#   확인 예: KOR 2026-08 cpi_index 126.5537 (2015=100), GBR 2026-05 cpi_yoy 3.0
#
# [물가 데이터플로를 3개 쓰는 이유 — 실측] 하나로는 20개국을 못 덮습니다.
#   · DSD_G20_PRICES@DF_G20_PRICES ("G20 - Consumer price indices, all items")
#     G20 19개국 + 유로존(REF_AREA=**EA**, EA20 아님) + EU27_2020 + G20 집계를
#     2026-08까지 제공. 전체(_T) 전용이라 근원 CPI는 없음. → cpi_index/cpi_yoy 1순위
#   · DSD_PRICES_COICOP2018@DF_PRICES_C2018_ALL (COICOP 2018)
#     CAN·DEU·FRA·ITA·JPN·MEX·SAU·TUR·ZAF가 2026-07/08까지 최신. 근원 있음.
#   · DSD_PRICES@DF_PRICES_ALL (COICOP 1999, 제안서가 가리킨 데이터플로)
#     AUS·DEU·GBR·KOR·USA 등은 여기 근원이 최신이지만 JPN(2021-06)·MEX(2024-07)·
#     EA20/FRA/ITA/TUR(2025-12)·RUS(2022-03)는 **정지 상태**. 근원 보강용으로만 사용.
#   같은 (지표, 국가)에 여러 데이터플로 후보가 생기므로 기준연도가 다른 지수를
#   이어 붙이지 않도록 **국가별로 데이터플로 하나를 골라** 그 시계열만 씁니다
#   (최신 기간 → METHODOLOGY N 우선 → 위 우선순위 순).
#   RU는 어느 데이터플로도 2022-03 이후가 없습니다(수집기가 World Bank 연간 폴백).
#
# [PPI 데이터플로 탐색 결과] OECD에는 별도의 "Producer price indices" 데이터플로가
# 없습니다. dataflow/all(1,548건) 전수 검색에서 이름/ID에 producer·output price·
# PPI가 들어간 항목이 0건이고, 실제 PPI는 단기경제지표의 MEASURE 코드
# `PP`(Producer prices)에 들어 있습니다.
#   확정: OECD.SDD.STES,DSD_KEI@DF_KEI
#         {AREA}.M.PP.IX.C._Z._Z (제조업 지수) / {AREA}.M.PP.GR.C._Z.GY (전년비)
#   ※ availableconstraint 조회 결과 이 시리즈의 TimeRange가 1947-01-01 ~
#     **2023-02-28**로 고정(38개국, 한국·일본·중국·인도 미포함). startPeriod=2024-01
#     요청은 NoRecordsFound입니다. 즉 과거 시계열 보강용이며 최신 PPI는
#     FRED(US)·Eurostat(EU)·ECOS(KR) 폴백이 필요합니다 → PPI_FROZEN_UNTIL + 로그.
#   (PPI가 없던 후보: DSD_STES@DF_INDSERV(생산·판매 물량), DSD_PRICES@DF_PRICES_*
#    전 계열(CPI/HICP 전용), DSD_PPP@DF_PPP_CPL(물가수준), DSD_RHPI@DF_RHPI_ALL)
#
# 국가별 실패 격리: 요청은 국가 묶음 단위이므로 배치 실패는 그 배치 대상 국가
# 전부에 ctx.record_error(...)로 기록하고 다음 배치로 넘어갑니다. 응답에 없는
# 국가는 조용히 건너뜁니다(수집기가 World Bank 연간 폴백을 씁니다).
# ============================================================
from __future__ import annotations

import time
from dataclasses import dataclass, field
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

SOURCE_NAME = "oecd"
CADENCE = "weekly"

SDMX_DATA_BASE = "https://sdmx.oecd.org/public/rest/data"

# 데이터플로 식별자 (agency,dataflow, 형태 — 뒤의 콤마까지가 OECD API 규약)
FLOW_G20_PRICES = "OECD.SDD.TPS,DSD_G20_PRICES@DF_G20_PRICES,"
FLOW_C2018_PRICES = "OECD.SDD.TPS,DSD_PRICES_COICOP2018@DF_PRICES_C2018_ALL,"
FLOW_C1999_PRICES = "OECD.SDD.TPS,DSD_PRICES@DF_PRICES_ALL,"
FLOW_KEI = "OECD.SDD.STES,DSD_KEI@DF_KEI,"

# 속도 제한 대응: 요청 사이 대기(초)와 429 재시도 대기(초).
REQUEST_SLEEP = 1.0
RATE_LIMIT_SLEEP = 30.0

# ctx.since가 없을 때의 기본 조회 시작(년). 전년비 파생·index100용으로 넉넉히.
DEFAULT_LOOKBACK_YEARS = 15

# KEI 생산자물가 시리즈가 멈춘 시점(availableconstraint 실측).
PPI_FROZEN_UNTIL = "2023-02"

# 물가 데이터플로의 차원 순서 (SDMX 키를 이 순서로 조립한다)
PRICES_DIMS = (
    "REF_AREA",
    "FREQ",
    "METHODOLOGY",
    "MEASURE",
    "UNIT_MEASURE",
    "EXPENDITURE",
    "ADJUSTMENT",
    "TRANSFORMATION",
)
KEI_DIMS = (
    "REF_AREA",
    "FREQ",
    "MEASURE",
    "UNIT_MEASURE",
    "ACTIVITY",
    "ADJUSTMENT",
    "TRANSFORMATION",
)

# 차원 조합 → 지표 id
_PRICES_MAP = {
    ("IX", "_T", "_Z"): "cpi_index",
    ("PA", "_T", "GY"): "cpi_yoy",
    ("PA", "_TXCP01_NRG", "GY"): "core_cpi_yoy",
}
_KEI_MAP = {
    ("IX", "_Z"): "ppi_index",
    ("GR", "GY"): "ppi_yoy",
}

UNITS = {
    "cpi_index": "index",
    "cpi_yoy": "%",
    "core_cpi_yoy": "%",
    "ppi_index": "index",
    "ppi_yoy": "%",
}

_METHODS = {
    "cpi_index": "OECD 소비자물가 원지수(월, 계절조정 없음, UNIT_MEASURE=IX)",
    "cpi_yoy": (
        "OECD가 제공하는 전년동월대비 변화율(TRANSFORMATION=GY)을 직접 사용 — "
        "지수에서 재계산하지 않음"
    ),
    "core_cpi_yoy": (
        "OECD가 제공하는 근원(식료품·에너지 제외, EXPENDITURE=_TXCP01_NRG) "
        "전년동월대비 변화율(TRANSFORMATION=GY) 직접 사용"
    ),
    "ppi_index": (
        "OECD 단기경제지표(KEI) 생산자물가 지수 — 제조업(ACTIVITY=C). "
        f"원천이 {PPI_FROZEN_UNTIL}까지만 제공"
    ),
    "ppi_yoy": (
        "OECD 단기경제지표(KEI) 생산자물가 전년동월비(TRANSFORMATION=GY) 직접 사용 — "
        f"제조업(ACTIVITY=C), 원천이 {PPI_FROZEN_UNTIL}까지만 제공"
    ),
}

# METHODOLOGY 선호 순서 (작을수록 우선). 국가 정의 지수를 조화지수보다 먼저 쓴다.
_METHODOLOGY_RANK = {"N": 0, "HICP": 1, "H": 2}


@dataclass(frozen=True)
class _Batch:
    """국가 묶음 SDMX 요청 1건.

    name        원본 저장·로그용 이름
    flow        데이터플로 식별자
    key_tail    REF_AREA 뒤에 붙는 키 꼬리
    dims        데이터플로의 차원 순서(단일 시리즈 키 조립용)
    dim_map     (구분 차원...) → 지표 id
    dim_cols    dim_map의 키를 만드는 CSV 열 이름
    indicators  이 배치가 담당하는 지표
    area_alias  registry의 oecd 코드 → 이 데이터플로의 REF_AREA 코드
    """

    name: str
    flow: str
    key_tail: str
    dims: tuple[str, ...]
    dim_map: dict[tuple[str, ...], str]
    dim_cols: tuple[str, ...]
    indicators: tuple[str, ...]
    area_alias: dict[str, str] = field(default_factory=dict)


BATCHES: tuple[_Batch, ...] = (
    _Batch(
        name="g20_prices",
        flow=FLOW_G20_PRICES,
        # METHODOLOGY는 국가마다 N/HICP로 달라 와일드카드로 둔다.
        key_tail=".M..CPI.IX+PA._T.N._Z+GY",
        dims=PRICES_DIMS,
        dim_map=_PRICES_MAP,
        dim_cols=("UNIT_MEASURE", "EXPENDITURE", "TRANSFORMATION"),
        indicators=("cpi_index", "cpi_yoy"),
        # 이 데이터플로의 유로존 코드는 EA20이 아니라 EA (실측).
        area_alias={"EA20": "EA"},
    ),
    _Batch(
        name="c2018_prices",
        flow=FLOW_C2018_PRICES,
        key_tail=".M..CPI.IX+PA._T+_TXCP01_NRG.N._Z+GY",
        dims=PRICES_DIMS,
        dim_map=_PRICES_MAP,
        dim_cols=("UNIT_MEASURE", "EXPENDITURE", "TRANSFORMATION"),
        indicators=("cpi_index", "cpi_yoy", "core_cpi_yoy"),
    ),
    _Batch(
        name="c1999_prices",
        flow=FLOW_C1999_PRICES,
        key_tail=".M..CPI.IX+PA._T+_TXCP01_NRG.N._Z+GY",
        dims=PRICES_DIMS,
        dim_map=_PRICES_MAP,
        dim_cols=("UNIT_MEASURE", "EXPENDITURE", "TRANSFORMATION"),
        indicators=("cpi_index", "cpi_yoy", "core_cpi_yoy"),
    ),
    _Batch(
        name="kei_ppi",
        flow=FLOW_KEI,
        key_tail=".M.PP.IX+GR.C._Z._Z+GY",
        dims=KEI_DIMS,
        dim_map=_KEI_MAP,
        dim_cols=("UNIT_MEASURE", "TRANSFORMATION"),
        indicators=("ppi_index", "ppi_yoy"),
    ),
)

SUPPORTED: tuple[str, ...] = ("cpi_index", "cpi_yoy", "core_cpi_yoy", "ppi_index", "ppi_yoy")


# ====================================================================== 내부 타입
@dataclass
class _Candidate:
    """한 (지표, 국가, 데이터플로, METHODOLOGY) 조합의 시계열 후보."""

    indicator: str
    iso: str
    flow: str
    series_key: str
    methodology: str
    batch_rank: int
    base_per: str = ""
    points: dict[str, float] = field(default_factory=dict)

    @property
    def last_period(self) -> str:
        return max(self.points) if self.points else ""

    @property
    def priority(self) -> tuple[str, int, int]:
        """정렬 키: 최신 기간이 늦을수록 / METHODOLOGY N / 배치 우선순위."""
        return (
            self.last_period,
            -_METHODOLOGY_RANK.get(self.methodology, 9),
            -self.batch_rank,
        )


# ====================================================================== 공개 함수
def series_url(flow: str, key: str, start_period: str) -> str:
    """브라우저에서도 열리는 SDMX-CSV 요청 URL (관측치 상세의 source_url)."""
    return (
        f"{SDMX_DATA_BASE}/{flow}/{key}"
        f"?startPeriod={start_period}&dimensionAtObservation=AllDimensions"
        "&format=csvfilewithlabels"
    )


def collect(
    countries: list[Any],
    indicators: list[Any],
    ctx: CollectContext,
) -> list[Observation]:
    """OECD 물가 지표를 국가 묶음으로 수집한다 (CONTRACT 7장).

    배치(요청 1건)가 실패하면 그 배치 대상 국가 전부에 오류를 기록하고 다음
    배치로 넘어간다. 같은 (지표, 국가)에 여러 데이터플로 후보가 있으면
    기준연도가 섞이지 않도록 하나만 골라 그 시계열 전체를 쓴다.
    """
    start_period = _start_period(ctx)
    candidates: dict[tuple[str, str, str, str], _Candidate] = {}
    requests_made = 0

    for batch_rank, batch in enumerate(BATCHES):
        wanted = [i for i in batch.indicators if _is_wanted(indicators, i)]
        if not wanted:
            continue
        areas = _batch_areas(countries, indicators, batch, wanted)
        if not areas:
            continue

        if requests_made:
            time.sleep(REQUEST_SLEEP)
        requests_made += 1
        key = "+".join(sorted(areas)) + batch.key_tail
        url = series_url(batch.flow, key, start_period)
        try:
            text = _fetch(ctx, url)
        except Exception as exc:  # noqa: BLE001 - 배치 단위 실패 격리
            for iso in sorted(set(areas.values())):
                ctx.record_error(SOURCE_NAME, iso, f"{batch.name} 요청 실패: {exc}")
            continue
        if not text:
            continue
        ctx.save_raw(SOURCE_NAME, f"{batch.name}_{start_period}", text)
        _absorb(text, batch, batch_rank, wanted, areas, candidates)

    obs = _pick_and_build(candidates, start_period)
    _log_coverage(ctx, countries, indicators, obs)
    return obs


# ====================================================================== 내부 구현
def _start_period(ctx: CollectContext) -> str:
    """조회 시작 월(`YYYY-MM`). ctx.since가 있으면 그 달부터."""
    if ctx.since is not None:
        return ctx.since.strftime("%Y-%m")
    return f"{date.today().year - DEFAULT_LOOKBACK_YEARS:04d}-01"


def _fetch(ctx: CollectContext, url: str) -> str | None:
    """SDMX-CSV 본문을 받아 온다. 429는 30초 뒤 1회만 재시도한다.

    OECD는 "데이터 없음"을 404 + 본문 `NoRecordsFound`로 돌려주므로 404는
    오류가 아니라 빈 결과로 취급한다.
    """
    for attempt in (0, 1):
        resp = ctx.get(url)
        if resp.status_code == 429:
            if attempt == 0:
                ctx.log(f"[{SOURCE_NAME}] 속도 제한(429) — {RATE_LIMIT_SLEEP:.0f}초 후 1회 재시도")
                time.sleep(RATE_LIMIT_SLEEP)
                continue
            raise RuntimeError("429 Too Many Requests (재시도 후에도 실패)")
        if resp.status_code == 404:
            ctx.log(f"[{SOURCE_NAME}] 해당 조건에 데이터 없음(404 NoRecordsFound)")
            return None
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}")
        return resp.text
    return None


def _batch_areas(
    countries: list[Any],
    indicators: list[Any],
    batch: _Batch,
    wanted: list[str],
) -> dict[str, str]:
    """배치 대상 국가를 `{REF_AREA 코드: iso}`로 모은다.

    base.iter_countries가 유로 공통 지표·`only` 제약을 적용하고, 데이터플로별
    REF_AREA 별칭(유로존 EA20→EA)을 여기서 흡수한다.
    """
    areas: dict[str, str] = {}
    for indicator_id in wanted:
        ind = find_indicator(indicators, indicator_id) or _FallbackIndicator(indicator_id)
        for country in iter_countries(countries, ind, SOURCE_NAME):
            code = country_code(country, "oecd")
            iso = str(field_of(country, "iso") or "").upper()
            if not code or not iso:
                continue
            code = str(code).upper()
            areas[batch.area_alias.get(code, code)] = iso
    return areas


def _absorb(
    text: str,
    batch: _Batch,
    batch_rank: int,
    wanted: list[str],
    areas: dict[str, str],
    candidates: dict[tuple[str, str, str, str], _Candidate],
) -> None:
    """응답 CSV를 (지표, 국가, 데이터플로, METHODOLOGY)별 시계열 후보로 쌓는다."""
    for row in sdmx_csv_to_rows(text):
        dims = tuple((row.get(c) or "").strip() for c in batch.dim_cols)
        indicator_id = batch.dim_map.get(dims)
        if indicator_id is None or indicator_id not in wanted:
            continue
        area = (row.get("REF_AREA") or "").strip().upper()
        iso = areas.get(area)
        period = (row.get("TIME_PERIOD") or "").strip()
        value = safe_float(row.get("OBS_VALUE"))
        if not iso or len(period) != 7 or value is None:
            continue
        methodology = (row.get("METHODOLOGY") or "").strip()
        ck = (indicator_id, iso, batch.flow, methodology)
        cand = candidates.get(ck)
        if cand is None:
            cand = _Candidate(
                indicator=indicator_id,
                iso=iso,
                flow=batch.flow,
                series_key=".".join((row.get(d) or "").strip() for d in batch.dims),
                methodology=methodology,
                batch_rank=batch_rank,
                base_per=(row.get("BASE_PER") or "").strip(),
            )
            candidates[ck] = cand
        cand.points[period] = value


def _pick_and_build(
    candidates: dict[tuple[str, str, str, str], _Candidate],
    start_period: str,
) -> list[Observation]:
    """(지표, 국가)마다 후보 1개를 골라 Observation 목록을 만든다."""
    best: dict[tuple[str, str], _Candidate] = {}
    for cand in candidates.values():
        if not cand.points:
            continue
        key = (cand.indicator, cand.iso)
        current = best.get(key)
        if current is None or cand.priority > current.priority:
            best[key] = cand

    obs: list[Observation] = []
    for cand in best.values():
        method = _METHODS[cand.indicator]
        if cand.base_per and cand.indicator.endswith("_index"):
            method = f"{method}. 기준연도 {cand.base_per}=100"
        if cand.methodology == "HICP":
            method = f"{method}. EU 조화지수(HICP) 기준"
        url = series_url(cand.flow, cand.series_key, start_period)
        for period in sorted(cand.points):
            obs.append(
                Observation(
                    indicator=cand.indicator,
                    iso=cand.iso,
                    freq="M",
                    period=period,
                    value=cand.points[period],
                    unit=UNITS[cand.indicator],
                    source=SOURCE_NAME,
                    series_id=f"{cand.flow.rstrip(',')}/{cand.series_key}",
                    source_url=url,
                    method=method,
                )
            )
    return obs


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
    """지표별로 값을 못 받은 국가를 한 줄로 알린다 (폴백 판단용)."""
    all_iso = {str(field_of(c, "iso") or "").upper() for c in countries or []} - {""}
    seen: dict[str, set[str]] = {}
    for ob in obs:
        seen.setdefault(ob.indicator, set()).add(ob.iso)
    for indicator_id in SUPPORTED:
        if not _is_wanted(indicators, indicator_id):
            continue
        missing = sorted(all_iso - seen.get(indicator_id, set()))
        if missing:
            ctx.log(f"[{SOURCE_NAME}] {indicator_id} 미수집 {len(missing)}개국: {','.join(missing)}")
    if seen.get("ppi_index") or seen.get("ppi_yoy"):
        ctx.log(
            f"[{SOURCE_NAME}] KEI 생산자물가는 원천이 {PPI_FROZEN_UNTIL}까지만 제공 — "
            "최신 PPI는 FRED/Eurostat/ECOS 폴백 필요"
        )
