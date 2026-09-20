# ============================================================
# [모듈 개요] webui/macro/registry.yaml·registry.py 계약 테스트
#
# CONTRACT.md 1장(국가 20개)·2장(지표 38개·열거값)·4장(SERIES 메타 키)을 코드가
# 실제로 지키는지 확인한다. 네트워크·AWS 접근은 없다.
# ============================================================
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "webui"))

from macro.registry import (  # noqa: E402
    AGGS,
    CATEGORIES,
    INDICATOR_IDS,
    UNITS,
    Registry,
    is_euro_shared,
    load_registry,
    validate,
)

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def registry() -> Registry:
    return load_registry()


def test_기본_경로에서_로드하고_검증을_통과한다(registry):
    validate(registry)  # 예외가 없으면 통과
    assert registry.path is not None
    assert registry.path.name == "registry.yaml"


def test_국가는_20개이고_iso_중복이_없다(registry):
    assert len(registry.countries) == 20
    isos = registry.iso_codes
    assert len(set(isos)) == 20
    for iso in ("KR", "US", "JP", "CN", "EU", "DE", "FR", "IT", "SA", "RU"):
        assert iso in isos


def test_지표는_38개이고_CONTRACT_고정_목록과_순서까지_같다(registry):
    assert len(registry.indicators) == 38
    assert len(INDICATOR_IDS) == 38
    assert tuple(registry.indicator_ids) == INDICATOR_IDS


def test_모든_지표의_열거값이_CONTRACT_2장_범위다(registry):
    for ind in registry.indicators:
        assert ind.unit in UNITS
        assert ind.category in CATEGORIES
        assert ind.agg in AGGS
        assert ind.higher_is == "neutral"
        assert ind.native_freq in ind.store_freqs


def test_유로존_국가_코드는_소스별_집계코드를_쓴다(registry):
    eu = registry.country("EU")
    assert (eu.code("bis"), eu.code("oecd"), eu.code("wb"), eu.code("imf")) == (
        "XM",
        "EA20",
        "EMU",
        "U2",
    )
    assert eu.euro is False  # 유로존 자체는 피참조 대상
    assert [c.iso for c in registry.euro_members()] == ["DE", "FR", "IT"]


def test_환율_심볼과_invert_규칙(registry):
    assert registry.country("KR").yahoo_fx == ("KRW=X", False)
    assert registry.country("EU").yahoo_fx == ("EURUSD=X", True)
    assert registry.country("GB").yahoo_fx == ("GBPUSD=X", True)
    assert registry.country("AU").yahoo_fx == ("AUDUSD=X", True)
    assert registry.country("JP").yahoo_fx == ("JPY=X", False)
    assert registry.country("US").yahoo_fx is None  # 기준통화
    # 유로 회원국은 EU와 같은 심볼 (수집기는 건너뛴다)
    assert registry.country("DE").yahoo_fx == ("EURUSD=X", True)
    assert registry.country("KR").code("fred_fx") == "DEXKOUS"
    assert registry.country("TR").code("fred_fx") is None


def test_country_code_헬퍼는_dict_코드도_문자열로_준다(registry):
    kr = registry.country("KR")
    assert kr.code("wb") == "KOR"
    assert kr.code("ember") == "South Korea"
    assert kr.code("owid") == "South Korea"
    assert kr.code("yahoo_fx") == "KRW=X"
    assert kr.code("없는소스") is None
    assert registry.country("TR").code("ember") == "Türkiye"


def test_그룹_조회(registry):
    assert len(registry.by_group("G20")) == 20
    assert [c.iso for c in registry.by_group("G7")] == ["US", "JP", "DE", "FR", "IT", "GB", "CA"]
    assert [c.iso for c in registry.by_group("BRICS")] == ["CN", "IN", "BR", "ZA", "RU"]


def test_유로_공통_지표_판정(registry):
    assert is_euro_shared("policy_rate")
    assert is_euro_shared("m2_level") and is_euro_shared("m2_yoy")
    assert is_euro_shared("fx_usd") and is_euro_shared("fx_value_index")
    assert not is_euro_shared("cpi_yoy")
    assert not is_euro_shared("house_price_index")
    assert not is_euro_shared("dxy")


def test_소스별_수집_국가에_유로_참조_규칙이_반영된다(registry):
    # BIS는 정책금리·환율의 원천 제공자 → 유로 회원국 제외 (EU만 수집)
    bis = [c.iso for c in registry.countries_for_source("bis")]
    assert "EU" in bis
    for iso in ("DE", "FR", "IT"):
        assert iso not in bis
    # 유로 회원국 고유 데이터(BIS 집값)는 지표를 명시하면 받는다
    hpi = [c.iso for c in registry.countries_for_source("bis", "house_price_index")]
    assert {"DE", "FR", "IT"} <= set(hpi)
    # IMF 통화량도 유로존 단일값
    assert "DE" not in [c.iso for c in registry.countries_for_source("imf")]
    # World Bank는 유로 공통 지표를 연간 폴백으로만 제공 → 국가별 지표 때문에 유지
    wb = [c.iso for c in registry.countries_for_source("worldbank")]
    assert {"DE", "FR", "IT", "EU"} <= set(wb)
    assert "DE" not in [c.iso for c in registry.countries_for_source("worldbank", "m2_yoy")]


def test_소스_코드_자리표시자가_없는_국가는_제외된다(registry):
    # fred 환율 폴백은 H.10 시리즈가 있는 국가만
    fred = [c.iso for c in registry.countries_for_source("fred")]
    assert {"US", "KR", "JP"} <= set(fred)
    for iso in ("TR", "SA", "AR", "ID", "RU"):
        assert iso not in fred
    # top_companies는 카탈로그 경로(only 4개국) + yahoo 경로(나머지)를 companies 이름으로 묶는다
    entries = registry.indicator("top_companies").source_entries("companies")
    assert entries[0]["only"] == ["KR", "JP", "US", "CN"]
    assert "only" not in entries[1]
    assert [c.iso for c in registry.countries_for_source("companies", "top_companies")] == (
        registry.iso_codes
    )


def test_소스별_지표_조회와_파생_via(registry):
    # energy_import_dep의 OWID 파생 폴백은 삭제됐다 (CSV에 primary_energy_production 열이 없음)
    assert [i.id for i in registry.indicators_for_source("owid")] == [
        "fuel_dep_oil",
        "fuel_dep_gas",
        "fuel_dep_coal",
    ]
    assert [e["name"] for e in registry.indicator("energy_import_dep").sorted_sources()] == [
        "worldbank"
    ]
    assert [i.id for i in registry.indicators_for_source("wits")] == ["exports_top_hs2"]
    assert [i.id for i in registry.indicators_for_source("ember")] == ["elec_mix"]
    assert [i.id for i in registry.indicators_for_source("cb_statements")] == ["cb_stance"]
    assert {"party_support", "gov_approval"} == {
        i.id for i in registry.indicators_for_source("wiki_polls")
    }
    bis_inds = {i.id for i in registry.indicators_for_source("bis")}
    assert {"policy_rate", "fx_usd", "house_price_index"} <= bis_inds


def test_지표_메타_플래그(registry):
    for cid in ("elec_mix", "exports_top_hs2", "top_companies"):
        assert registry.indicator(cid).is_composite, cid
    for did in ("cpi_yoy", "ppi_yoy", "m2_yoy", "house_price_yoy", "fx_value_index"):
        assert registry.indicator(did).is_derived, did
    assert registry.indicator("fuel_dep_oil").is_derived
    assert not registry.indicator("policy_rate").is_composite
    assert not registry.indicator("gdp_usd").is_derived
    assert registry.indicator("cpi_yoy").yoy_from == "cpi_index"
    assert registry.indicator("fx_value_index").derived_from == "fx_usd"


def test_소스_시리즈_문자열(registry):
    pol = registry.indicator("policy_rate")
    assert pol.source_entries("bis")[0]["series"] == "WS_CBPOL/1.0/D.{bis}"
    assert pol.source_entries("fred")[0]["only"] == ["US"]
    # WS_XRU 3번째 차원은 현지통화, COLLECTION은 E(기말) — bis.py 실측 확정
    assert registry.indicator("fx_usd").source_entries("bis")[0]["series"] == (
        "WS_XRU/1.0/M.{bis}.{ccy}.E"
    )
    assert registry.indicator("house_price_index").source_entries("bis")[0]["series"] == (
        "WS_SPP/1.0/Q.{bis}.N.628"
    )
    assert registry.indicator("gdp_usd").source_entries("worldbank")[0]["series"] == (
        "NY.GDP.MKTP.CD"
    )
    assert registry.indicator("dxy").source_entries("yahoo")[0]["series"] == "DX-Y.NYB"
    assert registry.indicator("dxy").source_entries("fred")[0]["series"] == "DTWEXBGS"
    # PPI는 FRED(US 최신) 1순위 → OECD KEI(2023-02에서 정지) 2순위 — oecd.py 실측 확정
    ppi = registry.indicator("ppi_index")
    assert ppi.source_entries("fred")[0]["series"] == "PPIACO"
    assert ppi.source_entries("oecd")[0]["series"] == "OECD.SDD.STES,DSD_KEI@DF_KEI"
    assert ppi.source_entries("oecd")[0]["key"] == "{oecd}.M.PP.IX.C._Z._Z"
    assert registry.indicator("ppi_yoy").source_entries("oecd")[0]["key"] == (
        "{oecd}.M.PP.GR.C._Z.GY"
    )
    # CPI 1순위는 G20 물가 데이터플로 (유로존 REF_AREA는 EA20이 아니라 EA — oecd.py가 별칭 처리)
    cpi = registry.indicator("cpi_index")
    assert "DSD_G20_PRICES@DF_G20_PRICES" in cpi.source_entries("oecd")[0]["series"]
    assert cpi.source_entries("oecd")[0]["key"] == "{oecd}.M..CPI.IX._T.N._Z"
    assert registry.indicator("cpi_yoy").source_entries("oecd")[0]["key"] == (
        "{oecd}.M..CPI.PA._T.N.GY"
    )
    assert registry.indicator("core_cpi_yoy").source_entries("oecd")[0]["key"] == (
        "{oecd}.M.N.CPI.PA._TXCP01_NRG.N.GY"
    )
    # m2_level은 IMF MFS_MA 단일 시리즈 키 + 자국통화 10억(lcu_bn) — imf.py M2_LEVEL_UNIT과 일치
    m2 = registry.indicator("m2_level")
    assert m2.unit == "lcu_bn" and "lcu_bn" in UNITS
    assert m2.source_entries("imf")[0]["series"] == "MFS_MA/{imf}.BM_MAI.XDC.M"
    assert m2.source_entries("fred")[0]["unit"] == "usd_bn"
    # WITS는 수출액(XPRT-TRD-VL)을 받아 비중을 계산한다 (HS 챕터 구간 16개 품목군)
    assert registry.indicator("exports_top_hs2").source_entries("wits")[0]["series"] == (
        "tradestats-trade/{wb}/product/all/indicator/XPRT-TRD-VL"
    )


def test_to_meta_items는_CONTRACT_4장_키를_만든다(registry):
    items = registry.to_meta_items()
    assert len(items) == 39  # 지표 38 + 국가 목록 1
    by_pk = {it["pk"]: it for it in items}
    assert by_pk["SERIES#policy_rate"]["sk"] == "META"
    assert by_pk["SERIES#policy_rate"]["unit"] == "%"
    assert by_pk["SERIES#policy_rate"]["store_freqs"] == ["D", "M", "Q", "Y"]
    assert by_pk["SERIES#policy_rate"]["updated_at"]
    for ind_id in INDICATOR_IDS:
        assert f"SERIES#{ind_id}" in by_pk
    countries_item = by_pk["SERIES#__countries__"]
    assert countries_item["sk"] == "META"
    assert len(countries_item["countries"]) == 20
    assert countries_item["countries"][0]["iso"] == "KR"
    assert "codes" in countries_item["countries"][0]


def test_api_meta는_필요한_필드만_노출한다(registry):
    meta = registry.indicator("cpi_yoy").to_api_meta()
    assert set(meta) == {
        "id",
        "name_ko",
        "unit",
        "category",
        "native_freq",
        "store_freqs",
        "agg",
        "decimals",
    }


def test_모르는_국가나_지표는_한국어_오류(registry):
    with pytest.raises(ValueError, match="등록되지 않은 국가"):
        registry.country("ZZ")
    with pytest.raises(ValueError, match="등록되지 않은 지표"):
        registry.indicator("없는지표")


def test_지표_누락은_차이를_표기한_ValueError(registry):
    broken = Registry(
        countries=list(registry.countries),
        indicators=[i for i in registry.indicators if i.id != "dxy"],
    )
    with pytest.raises(ValueError) as exc:
        validate(broken)
    msg = str(exc.value)
    assert "dxy" in msg
    assert "누락" in msg


def test_국가_수_불일치와_열거값_위반도_잡는다(registry):
    broken = Registry(countries=registry.countries[:19], indicators=list(registry.indicators))
    with pytest.raises(ValueError, match="국가 수는 20"):
        validate(broken)

    import copy

    bad_unit = copy.deepcopy(registry.indicators)
    bad_unit[0].unit = "won"
    bad_unit[0].store_freqs = ["D", "W"]  # native D보다 고빈도는 아니지만 W < M 순위 확인용
    with pytest.raises(ValueError) as exc:
        validate(Registry(countries=list(registry.countries), indicators=bad_unit))
    assert "unit" in str(exc.value)


def test_없는_파일_경로는_한국어_오류():
    with pytest.raises(ValueError, match="찾을 수 없다"):
        load_registry("/tmp/__없는__registry.yaml")
