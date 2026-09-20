# ============================================================
# [모듈 개요] OWID 에너지 CSV 소스 (fuel_dep_oil / fuel_dep_gas / fuel_dep_coal)
#
# Our World in Data의 연간 에너지 데이터셋(CSV 1개, 약 16MB)을 **배치당 1회만**
# 내려받아 연료별 해외의존도를 파생하고, Ember 키가 없을 때 쓸 발전 믹스 폴백
# 헬퍼(`elec_mix_from_owid`)를 제공합니다. 인증 없음, CC BY.
#
# [실측으로 확정한 것] (2026-09-20)
#   URL   https://owid-public.owid.io/data/energy/owid-energy-data.csv
#         → HTTP 200, text/csv, 15,926,881 bytes, 최신 연도 2025
#   코드북 https://raw.githubusercontent.com/owid/energy-data/master/
#          owid-energy-codebook.csv  (owid-public.owid.io 쪽 코드북 경로는 HTML
#          404를 돌려주므로 GitHub raw를 씁니다)
#   국가 매칭 열은 `iso_code`(ISO3). 유로존 행이 없어(OWID_EU27만 있음) 건너뜁니다.
#   연료별 생산·소비 열(모두 **TWh**, 코드북 unit으로 확인):
#     oil_production/oil_consumption, gas_production/gas_consumption,
#     coal_production/coal_consumption
#   발전 믹스 비중 열(%): coal_share_elec, gas_share_elec, oil_share_elec,
#     nuclear_share_elec, hydro_share_elec, wind_share_elec, solar_share_elec,
#     other_renewables_share_elec
#     ※ biofuel_share_elec는 other_renewables_share_elec에 **포함**되어 있어
#       (KOR 2025 두 값이 3.152로 동일) 같이 더하면 이중계상 → 쓰지 않습니다.
#       위 8개 합은 KOR 2025에서 99.99%로 100%에 수렴합니다.
#
# [energy_import_dep는 여기서 만들지 않습니다] 제안서는 OWID의
# `1 - 1차에너지생산/1차에너지소비`를 폴백으로 제시하지만, 실측 결과 이 CSV에는
# `primary_energy_consumption`만 있고 **`primary_energy_production` 열이 없습니다**
# (열 이름 전수 확인). 따라서 이 지표는 비워 두고 World Bank `EG.IMP.CONS.ZS`를
# 쓰도록 로그만 남깁니다 (ENERGY_IMPORT_DEP_NOTE).
#
# [연료 의존도 계산] max(0, 1 - 생산/소비) × 100  (단위 %, flags: estimated+derived)
#   OWID는 생산이 사실상 0인 국가(한국·일본의 원유·가스)의 생산 열을 결측으로
#   비워 둡니다. 소비가 있고 생산이 결측이면 생산 0으로 보아 의존도 100%로
#   계산하며(ASSUME_MISSING_PRODUCTION_ZERO), 그 가정을 method에 적습니다.
#   재수출·재고 변동을 반영하지 않는 추정치라 항상 `estimated` 플래그가 붙습니다.
#
# 국가별 실패 격리: CSV 1회 다운로드 실패는 전 국가 오류로 기록하고 빈 리스트를
# 돌려줍니다. 국가별 계산 실패는 그 국가만 기록하고 계속합니다.
# ============================================================
from __future__ import annotations

import io
import time
from pathlib import Path
from typing import Any

import pandas as pd

from macro.schema import Observation
from macro.sources.base import (
    LOCAL_RAW_DIR,
    CollectContext,
    country_code,
    field_of,
    find_indicator,
    iter_countries,
    safe_float,
)

SOURCE_NAME = "owid"
CADENCE = "monthly"

CSV_URL = "https://owid-public.owid.io/data/energy/owid-energy-data.csv"
CODEBOOK_URL = "https://raw.githubusercontent.com/owid/energy-data/master/owid-energy-codebook.csv"

# ctx.extra에 파싱된 DataFrame을 캐시하는 키 (같은 배치의 ember.py도 재사용)
EXTRA_CACHE_KEY = "owid_csv"

# dry-run 캐시: 로컬 사본이 이 시간(초) 안이면 다시 내려받지 않는다.
LOCAL_CACHE_PATH = LOCAL_RAW_DIR / "owid-energy-data.csv"
LOCAL_CACHE_MAX_AGE = 24 * 3600

# 16MB CSV라 기본 타임아웃(30초)보다 넉넉히 준다.
DOWNLOAD_TIMEOUT = 180

ENERGY_IMPORT_DEP_NOTE = (
    "OWID 에너지 CSV에는 primary_energy_production 열이 없어 energy_import_dep를 "
    "파생할 수 없습니다 — World Bank EG.IMP.CONS.ZS를 씁니다"
)

# 생산 열이 결측이면 0(국내 생산 없음)으로 간주한다.
ASSUME_MISSING_PRODUCTION_ZERO = True

# 지표 id → (생산 열, 소비 열, 연료 한글명)
FUEL_COLUMNS: dict[str, tuple[str, str, str]] = {
    "fuel_dep_oil": ("oil_production", "oil_consumption", "원유"),
    "fuel_dep_gas": ("gas_production", "gas_consumption", "천연가스"),
    "fuel_dep_coal": ("coal_production", "coal_consumption", "석탄"),
}

# 발전 믹스 폴백에 쓰는 비중 열 → 한글 라벨 (biofuel_share_elec는 이중계상 제외)
ELEC_SHARE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("coal_share_elec", "석탄"),
    ("gas_share_elec", "가스"),
    ("oil_share_elec", "기타화석"),
    ("nuclear_share_elec", "원자력"),
    ("hydro_share_elec", "수력"),
    ("wind_share_elec", "풍력"),
    ("solar_share_elec", "태양광"),
    ("other_renewables_share_elec", "기타재생"),
)

USE_COLUMNS: tuple[str, ...] = (
    "country",
    "year",
    "iso_code",
    "electricity_generation",
    *(c for pair in FUEL_COLUMNS.values() for c in pair[:2]),
    *(c for c, _ in ELEC_SHARE_COLUMNS),
)

SUPPORTED: tuple[str, ...] = tuple(FUEL_COLUMNS)

_METHOD_TEMPLATE = (
    "OWID 에너지 CSV 파생: max(0, 1 − {prod}/{cons}) × 100 "
    "({fuel_ko} 생산량·소비량 모두 TWh). 생산 {prod_twh}, 소비 {cons_twh} TWh. "
    "재수출·재고 변동 미반영 추정치"
)
_MISSING_PRODUCTION_NOTE = " (생산 열 결측 → 국내 생산 0으로 간주)"


# ====================================================================== 공개 함수
def load_dataframe(ctx: CollectContext) -> pd.DataFrame:
    """OWID 에너지 CSV를 1회만 받아 DataFrame으로 돌려준다 (ctx.extra 캐시).

    dry-run이면 로컬 `.macro_raw/owid-energy-data.csv`가 24시간 이내일 때
    재사용한다. 필요한 열만 읽어 메모리를 줄인다.
    """
    cached = ctx.extra.get(EXTRA_CACHE_KEY)
    if isinstance(cached, pd.DataFrame):
        return cached

    text: str | None = None
    if ctx.dry_run and _local_cache_fresh():
        ctx.log(f"[{SOURCE_NAME}] 로컬 캐시 재사용: {LOCAL_CACHE_PATH}")
        text = LOCAL_CACHE_PATH.read_text(encoding="utf-8")
    if text is None:
        resp = ctx.get(CSV_URL, timeout=DOWNLOAD_TIMEOUT)
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}")
        text = resp.text
        ctx.log(f"[{SOURCE_NAME}] CSV 다운로드 {len(text):,}바이트")
        if ctx.dry_run:
            _write_local_cache(ctx, text)

    df = parse_csv(text)
    ctx.extra[EXTRA_CACHE_KEY] = df
    return df


def parse_csv(text: str) -> pd.DataFrame:
    """OWID CSV 텍스트에서 필요한 열만 추려 DataFrame을 만든다."""
    header = pd.read_csv(io.StringIO(text), nrows=0)
    usecols = [c for c in USE_COLUMNS if c in header.columns]
    return pd.read_csv(io.StringIO(text), usecols=usecols)


def fuel_dependency(production: float | None, consumption: float | None) -> float | None:
    """`max(0, 1 − 생산/소비) × 100`. 소비가 없거나 0이면 None."""
    if consumption is None or consumption <= 0:
        return None
    prod = production
    if prod is None:
        if not ASSUME_MISSING_PRODUCTION_ZERO:
            return None
        prod = 0.0
    return max(0.0, 1.0 - prod / consumption) * 100.0


def elec_mix_from_owid(
    df: pd.DataFrame,
    iso3: str,
    year: int | None = None,
) -> tuple[int, dict[str, Any]] | None:
    """OWID 비중 열로 elec_mix payload를 만든다 (Ember 폴백용).

    `(연도, {"items": [...], "total_twh": ...})`를 돌려주고, 해당 국가·연도에
    비중 데이터가 없으면 None. year를 주지 않으면 비중이 있는 최신 연도를 쓴다.
    """
    share_cols = [c for c, _ in ELEC_SHARE_COLUMNS if c in df.columns]
    if not share_cols:
        return None
    rows = df[df["iso_code"] == iso3.upper()]
    rows = rows[rows[share_cols].notna().any(axis=1)]
    if year is not None:
        rows = rows[rows["year"] == int(year)]
    if rows.empty:
        return None
    row = rows.sort_values("year").iloc[-1]

    total_twh = safe_float(row.get("electricity_generation"))
    items: list[dict[str, Any]] = []
    for col, label in ELEC_SHARE_COLUMNS:
        share = safe_float(row.get(col)) if col in df.columns else None
        if share is None:
            continue
        item: dict[str, Any] = {"label": label, "value": round(share, 4)}
        if total_twh:
            item["twh"] = round(total_twh * share / 100.0, 4)
        items.append(item)
    if not items:
        return None
    items.sort(key=lambda it: it["value"], reverse=True)
    payload: dict[str, Any] = {"items": items}
    if total_twh:
        payload["total_twh"] = round(total_twh, 4)
    return int(row["year"]), payload


def collect(
    countries: list[Any],
    indicators: list[Any],
    ctx: CollectContext,
) -> list[Observation]:
    """연료별 해외의존도를 파생한다 (CONTRACT 7장).

    CSV는 배치당 1회만 받고, 국가별 계산 실패는 격리한다. 유로존은 OWID에
    해당 행이 없어 조용히 건너뛴다.
    """
    # energy_import_dep는 이 CSV로 만들 수 없다 → 조기 반환보다 먼저 알린다.
    if find_indicator(indicators, "energy_import_dep") is not None:
        ctx.log(f"[{SOURCE_NAME}] {ENERGY_IMPORT_DEP_NOTE}")
    wanted = [i for i in SUPPORTED if _is_wanted(indicators, i)]
    if not wanted:
        return []

    try:
        df = load_dataframe(ctx)
    except Exception as exc:  # noqa: BLE001 - 다운로드/파싱 실패는 전 국가 오류
        for country in countries or []:
            ctx.record_error(
                SOURCE_NAME, str(field_of(country, "iso") or "?"), f"CSV 로드 실패: {exc}"
            )
        return []

    _save_raw_subset(ctx, df, countries)

    obs: list[Observation] = []
    skipped: list[str] = []
    for indicator_id in wanted:
        ind = find_indicator(indicators, indicator_id) or _FallbackIndicator(indicator_id)
        prod_col, cons_col, fuel_ko = FUEL_COLUMNS[indicator_id]
        if prod_col not in df.columns and cons_col not in df.columns:
            ctx.log(f"[{SOURCE_NAME}] {indicator_id}: CSV에 {cons_col} 열이 없어 건너뜀")
            continue
        for country in iter_countries(countries, ind, SOURCE_NAME):
            iso = str(field_of(country, "iso") or "").upper()
            iso3 = owid_iso3(country)
            if not iso3:
                skipped.append(iso)
                continue
            try:
                obs.extend(
                    _country_obs(df, iso, iso3, indicator_id, prod_col, cons_col, fuel_ko, ctx)
                )
            except Exception as exc:  # noqa: BLE001 - 국가별 실패 격리
                ctx.record_error(SOURCE_NAME, iso, f"{indicator_id} 계산 실패: {exc}")
    if skipped:
        ctx.log(f"[{SOURCE_NAME}] OWID 코드 없음으로 건너뜀: {','.join(sorted(set(skipped)))}")
    return obs


def owid_iso3(country: Any) -> str:
    """registry의 `codes["owid"]`(문자열 또는 `{"iso3": ...}`)에서 ISO3를 뽑는다.

    유로존은 OWID에 국가 행이 없어(OWID_EU27 집계만 존재) 빈 문자열을 준다.
    """
    code = country_code(country, "owid")
    if isinstance(code, dict):
        code = code.get("iso3") or code.get("code")
    iso3 = str(code or "").upper()
    if not iso3 or iso3.startswith("OWID_") or len(iso3) != 3:
        return ""
    return iso3


# ====================================================================== 내부 구현
def _country_obs(
    df: pd.DataFrame,
    iso: str,
    iso3: str,
    indicator_id: str,
    prod_col: str,
    cons_col: str,
    fuel_ko: str,
    ctx: CollectContext,
) -> list[Observation]:
    """한 국가·한 연료의 연간 의존도 Observation 목록."""
    rows = df[df["iso_code"] == iso3]
    if rows.empty:
        return []
    since_year = ctx.since.year if ctx.since is not None else None
    obs: list[Observation] = []
    for _, row in rows.sort_values("year").iterrows():
        year = int(row["year"])
        if since_year is not None and year < since_year:
            continue
        prod = safe_float(row.get(prod_col)) if prod_col in df.columns else None
        cons = safe_float(row.get(cons_col)) if cons_col in df.columns else None
        value = fuel_dependency(prod, cons)
        if value is None:
            continue
        method = _METHOD_TEMPLATE.format(
            prod=prod_col,
            cons=cons_col,
            fuel_ko=fuel_ko,
            prod_twh="0(결측)" if prod is None else f"{prod:,.1f}",
            cons_twh=f"{cons:,.1f}" if cons is not None else "?",
        )
        if prod is None:
            method += _MISSING_PRODUCTION_NOTE
        obs.append(
            Observation(
                indicator=indicator_id,
                iso=iso,
                freq="Y",
                period=f"{year:04d}",
                value=value,
                unit="pct_share",
                source=SOURCE_NAME,
                series_id=f"owid-energy-data/{prod_col}|{cons_col}",
                source_url=CSV_URL,
                method=method,
                flags=["estimated", "derived"],
            )
        )
    return obs


def _save_raw_subset(ctx: CollectContext, df: pd.DataFrame, countries: list[Any]) -> None:
    """원본 보존(CONTRACT 8장). 16MB 전체 대신 **우리가 쓴 20개국·필요 열만** 남긴다.

    전체 CSV는 매달 3MB 가까운 gzip이 되고 우리가 쓰지 않는 열이 대부분이라,
    재현에 필요한 부분집합만 저장한다(원본 URL은 series 메타에 남는다).
    """
    try:
        iso3s = {owid_iso3(c) for c in countries or []} - {""}
        if not iso3s:
            return
        subset = df[df["iso_code"].isin(iso3s)]
        ctx.save_raw(SOURCE_NAME, "owid-energy-data-g20", subset.to_csv(index=False))
    except Exception as exc:  # noqa: BLE001 - 원본 보존 실패는 수집 실패가 아니다
        ctx.log(f"[{SOURCE_NAME}] 원본 부분집합 저장 실패: {exc}")


def _local_cache_fresh() -> bool:
    """dry-run 로컬 CSV 사본이 24시간 이내인지."""
    try:
        return (
            LOCAL_CACHE_PATH.is_file()
            and (time.time() - LOCAL_CACHE_PATH.stat().st_mtime) < LOCAL_CACHE_MAX_AGE
        )
    except OSError:
        return False


def _write_local_cache(ctx: CollectContext, text: str) -> None:
    """dry-run에서 다음 실행이 재사용할 로컬 사본을 남긴다 (실패 무시)."""
    try:
        Path(LOCAL_CACHE_PATH).parent.mkdir(parents=True, exist_ok=True)
        LOCAL_CACHE_PATH.write_text(text, encoding="utf-8")
    except OSError as exc:
        ctx.log(f"[{SOURCE_NAME}] 로컬 캐시 저장 실패: {exc}")


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
