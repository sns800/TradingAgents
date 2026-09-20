# ============================================================
# [모듈 개요] 매크로 정량 소스 B(oecd/imf/ember/owid/wits/companies) 단위 테스트
#
# 네트워크는 전부 스텁이며, 픽스처는 **실제 응답에서 잘라낸 것**입니다
# (tests/fixtures/macro/*, 각 20KB 이하 — 실측 URL은 각 소스 모듈 [모듈 개요] 참고).
#   oecd_g20_prices.csv    DF_G20_PRICES (KOR/USA/EA, 2026-06~08)
#   oecd_c2018_prices.csv  DF_PRICES_C2018_ALL (JPN 최신 2026-06~07, 근원 포함)
#   oecd_c1999_prices.csv  DF_PRICES_ALL (JPN **정지** 2021-04~06)
#   oecd_kei_ppi.csv       DSD_KEI@DF_KEI MEASURE=PP (USA/DEU/GBR, 2022-11~2023-02)
#   imf_mfs_ma.csv         MFS_MA BM_MAI.XDC.M (KOR/JPN, `YYYY-Mnn` 기간 형식)
#   ember_api_generation.json  Ember API GenerationResponse 형태
#   ember_yearly.csv       무키 공개 CSV (KOR + 유로존 Area='EU', 2024~2025)
#   owid_energy_small.csv  OWID 에너지 CSV 5개국 × 3년
#   wits_exports_kor.xml   WITS tradestats-trade 구조특화 XML (KOR 2023)
#   catalog_items.json     종목 카탈로그 항목 계약 샘플
#
# 검증 포인트: 복합값 payload 구조, 연료 의존도 계산, 라벨 매핑, Ember 키 없음
# 처리, Ember API의 유로존 집계 엔티티(`entity=EU`) 분리 요청·미제공 국가만 공개
# CSV 보충·인증 방식(쿼리 우선) 기억, 국가별 실패 격리, OECD의 "가장 최신
# 데이터플로 선택" 규칙.
# ============================================================
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "webui"))

from macro.schema import Observation  # noqa: E402
from macro.sources import companies, ember, imf, oecd, owid, wits  # noqa: E402

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "macro"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ====================================================================== 스텁
@dataclass
class FakeResponse:
    """requests.Response 대역 (소스 모듈이 쓰는 속성만)."""

    status_code: int = 200
    text: str = ""

    def json(self) -> Any:
        return json.loads(self.text)


@dataclass
class FakeCountry:
    """registry Country 대역 (base.py의 duck typing 계약)."""

    iso: str
    iso3: str = ""
    ccy: str = ""
    euro: bool = False
    codes: dict[str, Any] = field(default_factory=dict)


@dataclass
class FakeIndicator:
    """registry Indicator 대역."""

    id: str
    unit: str = ""
    sources: list[dict[str, Any]] = field(default_factory=list)


class StubContext:
    """CollectContext 대역.

    base.py가 아직 없어도(정량 소스 A 담당이 병행 작성) 테스트가 돌도록 같은
    인터페이스를 여기서 직접 구현한다: get/save_raw/record_error/since/dry_run/
    extra/log/errors.
    """

    def __init__(
        self,
        routes: dict[str, Any] | None = None,
        since: date | None = None,
        dry_run: bool = True,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.routes: dict[str, Any] = routes or {}
        self.since = since
        self.dry_run = dry_run
        self.no_llm = True
        self.extra: dict[str, Any] = extra if extra is not None else {}
        self.errors: list[str] = []
        self.logs: list[str] = []
        self.requests: list[tuple[str, dict[str, Any] | None, dict[str, str] | None]] = []
        self.raw: dict[str, Any] = {}

    # --- CollectContext 인터페이스 ---
    def get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: int = 30,
    ) -> FakeResponse:
        self.requests.append((url, params, headers))
        for token, resp in self.routes.items():
            if token in url:
                # 라우트 값이 호출 가능하면 요청 내용(파라미터·헤더)에 따라 분기한다.
                return resp(url, params, headers) if callable(resp) else resp
        return FakeResponse(status_code=404, text="NoRecordsFound")

    def save_raw(self, source: str, name: str, data: Any) -> str | None:
        self.raw[f"{source}/{name}"] = data
        return f"local://{source}/{name}"

    def record_error(self, source: str, iso: str, msg: str) -> str:
        entry = f"{source}:{iso}: {msg}"
        self.errors.append(entry)
        return entry

    def log(self, msg: str) -> None:
        self.logs.append(msg)

    # --- 테스트 도우미 ---
    def log_text(self) -> str:
        return "\n".join(self.logs)


@pytest.fixture(autouse=True)
def _isolate_owid_local_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """owid의 dry-run 로컬 캐시를 tmp로 돌린다.

    기본값은 작업 트리의 `.macro_raw/`라서, 격리하지 않으면 테스트가 저장소에
    파일을 남기고 다음 테스트가 그 캐시를 재사용해 스텁 HTTP를 건너뛴다.
    """
    monkeypatch.setattr(owid, "LOCAL_CACHE_PATH", tmp_path / "owid-energy-data.csv")


KR = FakeCountry(
    iso="KR",
    iso3="KOR",
    ccy="KRW",
    codes={
        "oecd": "KOR",
        "imf": "KOR",
        "wb": "KOR",
        "ember": {"name": "South Korea", "iso3": "KOR"},
        "owid": {"name": "South Korea", "iso3": "KOR"},
    },
)
US = FakeCountry(
    iso="US",
    iso3="USA",
    ccy="USD",
    codes={
        "oecd": "USA",
        "imf": "USA",
        "wb": "USA",
        "ember": {"name": "United States of America", "iso3": "USA"},
        "owid": {"name": "United States", "iso3": "USA"},
    },
)
JP = FakeCountry(
    iso="JP",
    iso3="JPN",
    ccy="JPY",
    codes={"oecd": "JPN", "imf": "JPN", "wb": "JPN", "owid": {"iso3": "JPN"}},
)
EU = FakeCountry(
    iso="EU",
    iso3="EA20",
    ccy="EUR",
    codes={
        "oecd": "EA20",
        "imf": "U2",
        "wb": "EMU",
        "ember": {"name": "EU"},
        "owid": {"name": "European Union (27)", "iso3": "OWID_EU27"},
    },
)
DE = FakeCountry(
    iso="DE",
    iso3="DEU",
    ccy="EUR",
    euro=True,
    codes={"oecd": "DEU", "imf": "DEU", "wb": "DEU", "owid": {"iso3": "DEU"}},
)


def prices_indicators() -> list[FakeIndicator]:
    return [
        FakeIndicator("cpi_index", "index"),
        FakeIndicator("cpi_yoy", "%"),
        FakeIndicator("core_cpi_yoy", "%"),
    ]


# ====================================================================== OECD
OECD_ROUTES = {
    "DSD_G20_PRICES@DF_G20_PRICES": FakeResponse(text=fixture("oecd_g20_prices.csv")),
    "DSD_PRICES_COICOP2018@DF_PRICES_C2018_ALL": FakeResponse(
        text=fixture("oecd_c2018_prices.csv")
    ),
    "DSD_PRICES@DF_PRICES_ALL": FakeResponse(text=fixture("oecd_c1999_prices.csv")),
    "DSD_KEI@DF_KEI": FakeResponse(text=fixture("oecd_kei_ppi.csv")),
}


def test_oecd_cpi_index_and_yoy_from_g20_dataflow() -> None:
    ctx = StubContext(OECD_ROUTES, since=date(2026, 6, 1))
    obs = oecd.collect([KR, US, EU], prices_indicators(), ctx)

    by_key = {(o.indicator, o.iso, o.period): o for o in obs}
    kr_index = by_key[("cpi_index", "KR", "2026-08")]
    assert kr_index.value == pytest.approx(126.5537)
    assert kr_index.unit == "index"
    assert kr_index.freq == "M"
    assert kr_index.source == "oecd"
    assert "DF_G20_PRICES" in kr_index.series_id
    assert "기준연도 2015=100" in kr_index.method

    kr_yoy = by_key[("cpi_yoy", "KR", "2026-08")]
    assert kr_yoy.unit == "%"
    # OECD가 제공하는 GY를 그대로 쓴다는 점을 method에 명시해야 한다.
    assert "TRANSFORMATION=GY" in kr_yoy.method
    assert "재계산하지 않음" in kr_yoy.method
    assert not kr_yoy.flags


def test_oecd_euro_area_uses_ea_alias_and_hicp_note() -> None:
    ctx = StubContext(OECD_ROUTES, since=date(2026, 6, 1))
    obs = oecd.collect([EU], prices_indicators(), ctx)

    eu_obs = [o for o in obs if o.iso == "EU"]
    assert eu_obs, "유로존은 DF_G20_PRICES의 REF_AREA=EA로 수집되어야 한다"
    # registry 코드는 EA20이지만 이 데이터플로는 EA를 쓴다 (별칭 적용 확인).
    g20_url = next(u for u, _, _ in ctx.requests if "DF_G20_PRICES" in u)
    assert "/EA." in g20_url and "EA20" not in g20_url
    assert any("HICP" in o.method for o in eu_obs)


def test_oecd_picks_freshest_dataflow_not_stale_one() -> None:
    """JPN은 COICOP1999(2021-06 정지)와 COICOP2018(2026-07) 양쪽에 있다."""
    ctx = StubContext(OECD_ROUTES, since=date(2021, 4, 1))
    obs = oecd.collect([JP], prices_indicators(), ctx)

    jp_index = [o for o in obs if o.indicator == "cpi_index" and o.iso == "JP"]
    assert jp_index, "JPN cpi_index를 수집해야 한다"
    assert max(o.period for o in jp_index) == "2026-07"
    # 기준연도가 다른 두 데이터플로를 이어 붙이면 안 된다 → 2021년 값은 없어야 한다.
    assert all(o.period >= "2026-06" for o in jp_index)
    assert all("C2018" in o.series_id for o in jp_index)


def test_oecd_core_cpi_only_from_dataflows_that_have_it() -> None:
    ctx = StubContext(OECD_ROUTES, since=date(2026, 6, 1))
    obs = oecd.collect([KR, US, EU, JP], prices_indicators(), ctx)

    core = {o.iso for o in obs if o.indicator == "core_cpi_yoy"}
    # DF_G20_PRICES는 전체(_T) 전용이라 KR/US/EU 근원은 없고, JPN은 C2018에 있다.
    assert core == {"JP"}
    assert "core_cpi_yoy 미수집" in ctx.log_text()


def test_oecd_ppi_from_kei_dataflow_with_frozen_warning() -> None:
    ctx = StubContext(OECD_ROUTES, since=date(2022, 11, 1))
    obs = oecd.collect(
        [US, DE],
        [FakeIndicator("ppi_index", "index"), FakeIndicator("ppi_yoy", "%")],
        ctx,
    )

    idx = [o for o in obs if o.indicator == "ppi_index"]
    yoy = [o for o in obs if o.indicator == "ppi_yoy"]
    assert idx and yoy
    assert {o.iso for o in idx} <= {"US", "DE"}
    assert all("DSD_KEI@DF_KEI" in o.series_id for o in idx)
    assert all(".M.PP.IX.C._Z._Z" in o.series_id for o in idx)
    assert all(".M.PP.GR.C._Z.GY" in o.series_id for o in yoy)
    assert all(o.unit == "index" for o in idx)
    assert all(o.unit == "%" for o in yoy)
    assert oecd.PPI_FROZEN_UNTIL in ctx.log_text()


def test_oecd_isolates_batch_failure_per_country() -> None:
    routes = dict(OECD_ROUTES)
    routes["DSD_G20_PRICES@DF_G20_PRICES"] = FakeResponse(status_code=500, text="boom")
    ctx = StubContext(routes, since=date(2026, 6, 1))
    obs = oecd.collect([KR, JP], prices_indicators(), ctx)

    # G20 배치만 실패 → 대상 국가별 오류 기록, 나머지 배치는 계속 진행한다.
    assert any("KR" in e and "HTTP 500" in e for e in ctx.errors)
    assert any(o.iso == "JP" for o in obs)


def test_oecd_rate_limit_retries_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}
    sleeps: list[float] = []
    monkeypatch.setattr(oecd.time, "sleep", lambda s: sleeps.append(s))

    class Flaky(StubContext):
        def get(self, url, params=None, headers=None, timeout=30):  # type: ignore[override]
            if "DF_G20_PRICES" in url:
                calls["n"] += 1
                if calls["n"] == 1:
                    return FakeResponse(status_code=429, text="")
                return FakeResponse(text=fixture("oecd_g20_prices.csv"))
            return super().get(url, params, headers, timeout)

    ctx = Flaky(OECD_ROUTES, since=date(2026, 6, 1))
    obs = oecd.collect([KR], [FakeIndicator("cpi_index", "index")], ctx)
    assert calls["n"] == 2
    assert oecd.RATE_LIMIT_SLEEP in sleeps
    assert any(o.indicator == "cpi_index" for o in obs)


# ====================================================================== IMF
IMF_ROUTES = {"IMF.STA/MFS_MA": FakeResponse(text=fixture("imf_mfs_ma.csv"))}


def test_imf_m2_level_converts_period_and_scales_to_billions() -> None:
    ctx = StubContext(IMF_ROUTES, since=date(2025, 8, 1))
    obs = imf.collect([KR, JP], [FakeIndicator("m2_level"), FakeIndicator("m2_yoy")], ctx)

    level = {(o.iso, o.period): o for o in obs if o.indicator == "m2_level"}
    kr = level[("KR", "2025-10")]  # 원천은 `2025-M10`
    assert kr.value == pytest.approx(4_472_179.82, rel=1e-9)  # 4.47215e15 KRW / 1e9
    assert kr.unit == "lcu_bn" == imf.M2_LEVEL_UNIT
    assert kr.freq == "M"
    assert kr.series_id == "IMF.STA/MFS_MA/KOR.BM_MAI.XDC.M"
    assert "c[TIME_PERIOD]=ge:" in kr.source_url
    assert "SCALE" in kr.method  # 스케일 미적용을 명시


def test_imf_m2_yoy_is_derived_from_12_months_earlier() -> None:
    ctx = StubContext(IMF_ROUTES, since=date(2025, 10, 1))
    obs = imf.collect([JP], [FakeIndicator("m2_yoy")], ctx)

    yoy = {o.period: o for o in obs if o.indicator == "m2_yoy"}
    assert yoy, "12개월 전 값이 있는 기간은 전년비가 나와야 한다"
    sample = next(iter(yoy.values()))
    assert sample.unit == "%"
    assert sample.flags == ["derived"]
    # 전년비를 만들려면 요청 시작보다 12개월 더 받아야 한다.
    url = ctx.requests[0][0]
    assert "ge:2024-10" in url


def test_imf_wildcard_free_key_and_euro_member_skipped() -> None:
    ctx = StubContext(IMF_ROUTES, since=date(2025, 8, 1))
    imf.collect([KR, JP, DE], [FakeIndicator("m2_level")], ctx)

    url = ctx.requests[0][0]
    # 와일드카드가 동작하지 않으므로 4개 차원을 모두 채워야 한다.
    assert url.split("/+/")[1].startswith("JPN+KOR.BM_MAI.XDC.M")
    # 유로 회원국은 base.iter_countries가 제외한다(EU 값 참조).
    assert "DEU" not in url


def test_imf_records_error_on_http_failure() -> None:
    ctx = StubContext({"IMF.STA/MFS_MA": FakeResponse(status_code=503, text="")})
    obs = imf.collect([KR, JP], [FakeIndicator("m2_level")], ctx)
    assert obs == []
    assert len(ctx.errors) == 2
    assert all("HTTP 503" in e for e in ctx.errors)


def test_imf_normalize_period() -> None:
    assert imf.normalize_period("2025-M10") == "2025-10"
    assert imf.normalize_period("2025-M1") == "2025-01"
    assert imf.normalize_period("2025-10") == "2025-10"
    assert imf.normalize_period("2025-M13") is None
    assert imf.normalize_period("2025") is None


# ====================================================================== Ember
EMBER_CSV_ROUTES = {
    "release_generation_yearly_global.csv": FakeResponse(text=fixture("ember_yearly.csv")),
}
ELEC_MIX = [FakeIndicator("elec_mix", "pct_share")]


def test_ember_without_api_key_uses_public_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ember.API_KEY_ENV, raising=False)
    ctx = StubContext(EMBER_CSV_ROUTES, since=date(2025, 1, 1))
    obs = ember.collect([KR, US, EU], ELEC_MIX, ctx)

    assert f"{ember.API_KEY_ENV} 없음" in ctx.log_text()
    # 키가 없으면 API를 아예 부르지 않는다.
    assert not any("api.ember-energy.org" in u for u, _, _ in ctx.requests)

    kr = next(o for o in obs if o.iso == "KR" and o.freq == "Y")
    assert kr.value is None and kr.payload is not None
    assert kr.unit == "pct_share"
    assert kr.flags == ["fallback_source"]
    labels = [it["label"] for it in kr.payload["items"]]
    assert labels[0] == "석탄"  # 비중 내림차순
    assert set(labels) <= set(ember.FUEL_LABELS.values())
    assert "순수입" not in labels and "Net imports" not in labels
    assert sum(it["value"] for it in kr.payload["items"]) == pytest.approx(100.0, abs=0.05)
    assert kr.payload["total_twh"] == pytest.approx(624.67, abs=0.1)
    assert all("twh" in it for it in kr.payload["items"])


def test_ember_matches_euro_area_by_area_name() -> None:
    ctx = StubContext(EMBER_CSV_ROUTES, since=date(2025, 1, 1))
    obs = ember.collect([EU], ELEC_MIX, ctx)
    eu = [o for o in obs if o.iso == "EU"]
    assert eu, "공개 CSV의 Area='EU' 행(ISO 3 code 없음)을 이름으로 맞춰야 한다"
    assert eu[0].payload["items"][0]["label"] in ember.FUEL_LABELS.values()


def ember_api_doc(entities: list[tuple[str, str | None]]) -> str:
    """`ember_api_generation.json`(KOR 1개국)의 행을 엔티티별로 복제한 API 응답.

    유로존 집계 엔티티는 실제 응답처럼 `entity_code`가 null이다(실측).
    """
    base = json.loads(fixture("ember_api_generation.json"))
    rows = [
        {**row, "entity": name, "entity_code": code}
        for name, code in entities
        for row in base["data"]
    ]
    return json.dumps({"stats": {"rows": len(rows)}, "data": rows}, ensure_ascii=False)


def test_ember_api_path_parses_generation_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ember.API_KEY_ENV, "test-key")
    routes = {
        "electricity-generation/yearly": FakeResponse(
            text=fixture("ember_api_generation.json")
        ),
        **EMBER_CSV_ROUTES,
    }
    ctx = StubContext(routes, since=date(2025, 1, 1))
    obs = ember.collect([KR, US], ELEC_MIX, ctx)

    api_calls = [(u, p, h) for u, p, h in ctx.requests if "api.ember-energy.org" in u]
    assert api_calls
    _, params, headers = api_calls[0]
    # OpenAPI가 공식으로 선언한 쿼리 파라미터를 첫 요청부터 쓴다(헤더는 403 실측).
    assert headers is None
    assert params["api_key"] == "test-key"
    assert params["entity_code"] == "KOR,USA"
    assert params["is_aggregate_series"] == "false"
    assert ctx.extra[ember.AUTH_EXTRA_KEY] == ember.AUTH_QUERY

    kr = next(o for o in obs if o.iso == "KR" and o.freq == "Y" and not o.flags)
    assert kr.period == "2025"
    assert kr.payload["items"][0]["label"] == "석탄"


def test_ember_api_requests_euro_area_by_entity_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """유로존은 entity_code 옵션에 없고 집계 엔티티 `entity=EU`로만 온다(실측).

    entity와 entity_code를 한 요청에 같이 넣으면 AND로 걸려 0행이므로 요청이
    나뉘어야 한다.
    """
    monkeypatch.setenv(ember.API_KEY_ENV, "test-key")

    def api_route(url: str, params: dict[str, Any] | None, headers: Any) -> FakeResponse:
        params = params or {}
        assert not (params.get("entity") and params.get("entity_code")), (
            "entity와 entity_code를 같은 요청에 넣으면 API가 0행을 준다"
        )
        if params.get("entity"):
            return FakeResponse(text=ember_api_doc([("EU", None)]))
        return FakeResponse(text=ember_api_doc([("South Korea", "KOR")]))

    ctx = StubContext(
        {"electricity-generation/yearly": api_route, **EMBER_CSV_ROUTES},
        since=date(2025, 1, 1),
    )
    obs = ember.collect([KR, EU], ELEC_MIX, ctx)

    api_params = [
        p for u, p, _ in ctx.requests if u.endswith("electricity-generation/yearly")
    ]
    assert [p.get("entity_code") for p in api_params if p.get("entity_code")] == ["KOR"]
    assert [p.get("entity") for p in api_params if p.get("entity")] == ["EU"]
    # API가 유로존을 주면 보충은 필요 없다 → flags 없음.
    eu = next(o for o in obs if o.iso == "EU" and o.freq == "Y")
    assert eu.flags == [] and eu.method == ember.METHOD_API
    assert "API 미제공" not in ctx.log_text()
    assert not any("release_generation_yearly" in u for u, _, _ in ctx.requests)


def test_ember_supplements_api_missing_country_from_public_csv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """API가 주지 않은 국가(유로존)만 공개 CSV로 보충한다 — 나머지는 flags 없음."""
    monkeypatch.setenv(ember.API_KEY_ENV, "test-key")
    api_doc = ember_api_doc([("South Korea", "KOR"), ("United States", "USA")])

    def api_route(url: str, params: dict[str, Any] | None, headers: Any) -> FakeResponse:
        if (params or {}).get("entity"):  # 집계 엔티티를 못 주는 상황
            return FakeResponse(status_code=500, text="no aggregate entity")
        return FakeResponse(text=api_doc)

    ctx = StubContext(
        {"electricity-generation/yearly": api_route, **EMBER_CSV_ROUTES},
        since=date(2025, 1, 1),
    )
    obs = ember.collect([KR, US, EU], ELEC_MIX, ctx)

    assert {o.iso for o in obs} == {"KR", "US", "EU"}
    assert "API 미제공 1개국 → 공개 CSV 보충: EU" in ctx.log_text()

    eu = [o for o in obs if o.iso == "EU"]
    assert eu and all(o.flags == ["fallback_source"] for o in eu)
    assert all("API 미제공 국가 보충" in o.method for o in eu)
    assert eu[0].source == "ember" and eu[0].payload["items"]
    # 보충 대상이 아닌 국가는 API 관측치 그대로(플래그 없음)
    assert all(o.flags == [] for o in obs if o.iso in {"KR", "US"})
    # 공개 CSV는 해상도당 1회만 내려받는다(ctx.extra 캐시)
    csv_calls = [u for u, _, _ in ctx.requests if "release_generation_yearly" in u]
    assert len(csv_calls) == 1


def test_ember_skips_csv_supplement_when_api_serves_other_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ember에 그 해상도 시계열이 없는 국가(ID·SA 월간 — 실측)는 보충하지 않는다.

    API가 연간을 줬다면 월간이 비는 건 원천이 없기 때문이라 CSV에도 없다 →
    28MB 월간 CSV를 매 실행 헛되게 내려받지 않는다.
    """
    monkeypatch.setenv(ember.API_KEY_ENV, "test-key")
    empty = json.dumps({"stats": {"rows": 0}, "data": []})
    ctx = StubContext(
        {
            "electricity-generation/yearly": FakeResponse(
                text=ember_api_doc([("South Korea", "KOR")])
            ),
            "electricity-generation/monthly": FakeResponse(text=empty),
            **EMBER_CSV_ROUTES,
        },
        since=date(2025, 1, 1),
    )
    obs = ember.collect([KR], ELEC_MIX, ctx)

    assert {o.freq for o in obs} == {"Y"}
    assert "CSV 보충 생략: KR" in ctx.log_text()
    assert "API 미제공" not in ctx.log_text()
    assert not any("release_generation" in u for u, _, _ in ctx.requests)


def test_ember_api_switches_auth_mode_once_and_remembers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """헤더 거부(403)를 겪으면 쿼리로 전환하고 그 방식을 기억해 재시도를 없앤다."""
    monkeypatch.setenv(ember.API_KEY_ENV, "test-key")
    doc = ember_api_doc([("South Korea", "KOR")])

    def api_route(url: str, params: dict[str, Any] | None, headers: Any) -> FakeResponse:
        if headers and headers.get("X-API-Key"):
            return FakeResponse(status_code=403, text='{"detail":"No API key set"}')
        return FakeResponse(text=doc)

    ctx = StubContext(
        {
            "electricity-generation/yearly": api_route,
            "electricity-generation/monthly": api_route,
            **EMBER_CSV_ROUTES,
        },
        since=date(2025, 1, 1),
        extra={ember.AUTH_EXTRA_KEY: ember.AUTH_HEADER},  # 지난 요청이 헤더였다고 가정
    )
    ember.collect([KR], ELEC_MIX, ctx)

    api_calls = [(p, h) for u, p, h in ctx.requests if "api.ember-energy.org" in u]
    assert api_calls[0][1] == {"X-API-Key": "test-key"}  # 기억된 방식 → 403
    assert api_calls[1][0]["api_key"] == "test-key"  # 1회 전환
    assert ctx.extra[ember.AUTH_EXTRA_KEY] == ember.AUTH_QUERY
    # 기억 이후(월간) 요청은 헤더를 다시 시도하지 않는다 → 해상도당 1회
    assert [h for _, h in api_calls[2:]] == [None]
    assert len(api_calls) == 3


def test_ember_full_csv_fallback_when_api_request_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """API 요청 자체가 실패하면 예전처럼 전 국가를 공개 CSV에서 받는다."""
    monkeypatch.setenv(ember.API_KEY_ENV, "test-key")
    routes = {
        "electricity-generation": FakeResponse(status_code=500, text="boom"),
        **EMBER_CSV_ROUTES,
    }
    ctx = StubContext(routes, since=date(2025, 1, 1))
    obs = ember.collect([KR, EU], ELEC_MIX, ctx)

    assert "공개 CSV로 폴백" in ctx.log_text()
    assert {o.iso for o in obs} == {"KR", "EU"}
    assert all(o.flags == ["fallback_source"] for o in obs)
    assert all(o.method == ember.METHOD_CSV for o in obs)
    assert "API 미제공" not in ctx.log_text()
    # 5xx는 인증 문제가 아니므로 인증 방식 재시도를 하지 않는다(해상도당 1회).
    assert len([u for u, _, _ in ctx.requests if "api.ember-energy.org" in u]) == 2


def test_ember_label_mapping_and_exclusions() -> None:
    assert ember.label_for("Coal") == "석탄"
    assert ember.label_for("Other renewables") == "기타재생"
    assert ember.label_for("Net imports") is None
    assert ember.label_for("") is None
    # 미등록 전원은 영문 원문 그대로(fail-open)
    assert ember.label_for("Tidal") == "Tidal"


def test_ember_falls_back_to_owid_when_all_paths_fail() -> None:
    ctx = StubContext(
        {"owid-energy-data.csv": FakeResponse(text=fixture("owid_energy_small.csv"))},
        since=date(2023, 1, 1),
    )
    obs = ember.collect([KR], ELEC_MIX, ctx)

    assert obs, "Ember가 전부 막히면 OWID로 폴백해야 한다"
    kr = obs[0]
    assert kr.source == "owid"
    assert "fallback_source" in kr.flags and "estimated" in kr.flags
    assert kr.freq == "Y"
    assert kr.payload["items"]


# ====================================================================== OWID
OWID_ROUTES = {"owid-energy-data.csv": FakeResponse(text=fixture("owid_energy_small.csv"))}
FUEL_INDICATORS = [
    FakeIndicator("fuel_dep_oil", "pct_share"),
    FakeIndicator("fuel_dep_gas", "pct_share"),
    FakeIndicator("fuel_dep_coal", "pct_share"),
]


@pytest.mark.parametrize(
    ("production", "consumption", "expected"),
    [
        (0.0, 100.0, 100.0),  # 생산 없음 → 전량 수입
        (40.0, 100.0, 60.0),
        (100.0, 100.0, 0.0),
        (150.0, 100.0, 0.0),  # 순수출국도 음수가 아니라 0
        (None, 100.0, 100.0),  # 생산 열 결측 → 0으로 간주
        (10.0, 0.0, None),  # 소비가 0이면 정의되지 않음
        (10.0, None, None),
    ],
)
def test_owid_fuel_dependency_formula(
    production: float | None, consumption: float | None, expected: float | None
) -> None:
    got = owid.fuel_dependency(production, consumption)
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected)


def test_owid_fuel_dependency_observations() -> None:
    ctx = StubContext(OWID_ROUTES, since=date(2024, 1, 1))
    obs = owid.collect([KR, US, EU], FUEL_INDICATORS, ctx)

    by_key = {(o.indicator, o.iso, o.period): o for o in obs}
    kr_oil = by_key[("fuel_dep_oil", "KR", "2024")]
    # 한국은 OWID에 oil_production이 결측 → 생산 0으로 보아 100%
    assert kr_oil.value == pytest.approx(100.0)
    assert kr_oil.unit == "pct_share"
    assert kr_oil.freq == "Y"
    assert sorted(kr_oil.flags) == ["derived", "estimated"]
    assert "max(0, 1 − oil_production/oil_consumption) × 100" in kr_oil.method
    assert "TWh" in kr_oil.method
    assert "생산 열 결측" in kr_oil.method

    us_coal = by_key[("fuel_dep_coal", "US", "2024")]
    assert us_coal.value == pytest.approx(0.0)  # 미국은 석탄 순수출
    assert "생산 열 결측" not in us_coal.method

    # 유로존은 OWID에 국가 행이 없어(OWID_EU27 집계만) 건너뛴다.
    assert not any(o.iso == "EU" for o in obs)
    assert "OWID 코드 없음으로 건너뜀: EU" in ctx.log_text()


def test_owid_does_not_derive_energy_import_dep() -> None:
    ctx = StubContext(OWID_ROUTES, since=date(2024, 1, 1))
    obs = owid.collect([KR], [FakeIndicator("energy_import_dep", "pct_share")], ctx)
    assert obs == []
    assert owid.ENERGY_IMPORT_DEP_NOTE in ctx.log_text()


def test_owid_caches_dataframe_in_ctx_extra() -> None:
    ctx = StubContext(OWID_ROUTES, since=date(2024, 1, 1))
    owid.collect([KR, US], FUEL_INDICATORS, ctx)
    downloads = [u for u, _, _ in ctx.requests if "owid-energy-data.csv" in u]
    assert len(downloads) == 1, "대용량 CSV는 배치당 1회만 내려받아야 한다"
    assert owid.EXTRA_CACHE_KEY in ctx.extra


def test_owid_elec_mix_helper_excludes_biofuel_double_count() -> None:
    ctx = StubContext(OWID_ROUTES)
    df = owid.load_dataframe(ctx)
    got = owid.elec_mix_from_owid(df, "KOR", 2024)
    assert got is not None
    year, payload = got
    assert year == 2024
    labels = [it["label"] for it in payload["items"]]
    assert "바이오" not in labels  # other_renewables에 포함되어 있어 제외
    assert "기타재생" in labels
    assert sum(it["value"] for it in payload["items"]) == pytest.approx(100.0, abs=0.5)
    assert owid.elec_mix_from_owid(df, "ZZZ") is None


def test_owid_records_error_when_download_fails() -> None:
    ctx = StubContext({"owid-energy-data.csv": FakeResponse(status_code=502, text="")})
    obs = owid.collect([KR, US], FUEL_INDICATORS, ctx)
    assert obs == []
    assert len(ctx.errors) == 2


# ====================================================================== WITS
WITS_ROUTES = {"reporter/kor/year/2023": FakeResponse(text=fixture("wits_exports_kor.xml"))}


def test_wits_exports_top_hs2_payload() -> None:
    ctx = StubContext(WITS_ROUTES, since=date(2023, 1, 1))
    obs = wits.collect([KR], [FakeIndicator("exports_top_hs2", "pct_share")], ctx)

    assert len(obs) == 1
    ob = obs[0]
    assert ob.indicator == "exports_top_hs2"
    assert ob.iso == "KR"
    assert ob.freq == "Y"
    assert ob.period == "2023"
    assert ob.value is None
    assert ob.unit == "pct_share"
    assert ob.source == "wits"

    items = ob.payload["items"]
    assert len(items) == wits.TOP_N
    top = items[0]
    assert top["hs2"] == "84-85"
    assert top["label"] == top["label_ko"] == "기계·전기기기"
    assert top["label_en"] == "Mach and Elec"
    assert top["value"] == pytest.approx(38.62, abs=0.01)
    assert top["usd_mn"] == pytest.approx(243_992.943, abs=0.01)
    # 실측 검증: 16개 품목군 합 = WITS의 Total 시리즈와 일치 (6,318억 달러)
    assert ob.payload["total_usd_mn"] == pytest.approx(631_804.231, abs=0.01)
    assert ob.payload["n_groups"] == 16
    assert items == sorted(items, key=lambda it: -it["value"])


def test_wits_single_chapter_group_uses_hs2_korean_label() -> None:
    assert wits.sector_labels("27-27_Fuels") == ("27", "광물성 연료·에너지", "Fuels")
    assert wits.sector_labels("84-85_MachElec") == ("84-85", "기계·전기기기", "Mach and Elec")
    # SITC·가공단계 그룹과 Total은 HS 챕터 구간이 아니라 제외한다.
    assert wits.sector_labels("UNCTAD-SoP1") is None
    assert wits.sector_labels("Total") is None
    assert wits.sector_labels("manuf") is None
    assert wits.hs2_label_ko("85") == "전기기기·전자"
    assert wits.hs2_label_ko("5") is None  # 표에 없는 챕터 → 영문 fail-open


def test_wits_skips_reporters_without_iso3_and_walks_back_years() -> None:
    ctx = StubContext(WITS_ROUTES, since=date(2025, 1, 1))
    obs = wits.collect([KR, EU], [FakeIndicator("exports_top_hs2")], ctx)

    # 최신 연도가 없으면 이전 연도로 내려가며 찾는다 (2023에서 성공).
    assert [o.period for o in obs] == ["2023"]
    years = [u.split("/year/")[1].split("/")[0] for u, _, _ in ctx.requests]
    assert years[0] > "2023" and "2023" in years
    # EU(EMU)는 WITS 리포터가 아니므로 오류가 아니라 "데이터 없음" 로그로 끝난다.
    assert not any(o.iso == "EU" for o in obs)
    assert "exports_top_hs2 미수집" in ctx.log_text()


def test_wits_isolates_country_failure() -> None:
    routes = {"reporter/kor": FakeResponse(status_code=500, text="")}
    ctx = StubContext(routes, since=date(2023, 1, 1))
    obs = wits.collect([KR], [FakeIndicator("exports_top_hs2")], ctx)
    assert obs == []
    assert any("wits:KR" in e and "HTTP 500" in e for e in ctx.errors)


def test_wits_reuses_discovered_year_across_countries() -> None:
    routes = {
        "reporter/kor/year/2023": FakeResponse(text=fixture("wits_exports_kor.xml")),
        "reporter/usa/year/2023": FakeResponse(text=fixture("wits_exports_kor.xml")),
    }
    ctx = StubContext(routes, since=date(2025, 1, 1))
    wits.collect([KR, US], [FakeIndicator("exports_top_hs2")], ctx)
    assert ctx.extra[wits.EXTRA_YEAR_KEY] == 2023
    usa_years = [
        u.split("/year/")[1].split("/")[0] for u, _, _ in ctx.requests if "reporter/usa" in u
    ]
    assert usa_years == ["2023"], "연도 탐색 결과를 재사용해 요청 수를 줄여야 한다"


# ====================================================================== companies
CATALOG = json.loads(fixture("catalog_items.json"))
FX = {"KRW": 1380.0, "USD": 1.0}
TOP_COMPANIES = [FakeIndicator("top_companies", "usd_bn")]


def test_companies_from_catalog_ranks_by_market_cap() -> None:
    ctx = StubContext(
        extra={"catalog_loader": lambda market: CATALOG.get(market, []), "fx_rates": FX}
    )
    obs = companies.collect([KR, US], TOP_COMPANIES, ctx)

    kr = next(o for o in obs if o.iso == "KR")
    assert kr.indicator == "top_companies"
    assert kr.freq == "Q"
    assert kr.period == companies.current_quarter()
    assert kr.value is None
    assert kr.unit == "usd_bn"
    assert kr.source == "catalog"
    assert kr.payload["source_detail"] == "catalog"
    assert kr.payload["asof"]

    items = kr.payload["items"]
    # market_cap이 None인 종목은 제외되고, 나머지는 시총 내림차순
    assert [it["ticker"] for it in items] == ["005930", "000660", "373220", "005380", "068270"]
    assert items[0]["name"] == "삼성전자"  # name_ko 우선
    assert items[0]["currency"] == "KRW"
    assert items[0]["market_cap_local"] == pytest.approx(525e12)
    assert items[0]["market_cap_usd_bn"] == pytest.approx(525e12 / 1380.0 / 1e9)
    assert set(items[0]) == {
        "label",
        "value",
        "name",
        "ticker",
        "sector",
        "market_cap_usd_bn",
        "market_cap_local",
        "currency",
    }
    # label/value는 복합값 3종이 공유하는 공통 쌍
    assert items[0]["label"] == items[0]["name"]
    assert items[0]["value"] == items[0]["market_cap_usd_bn"]

    us = next(o for o in obs if o.iso == "US")
    assert us.payload["items"][0]["market_cap_usd_bn"] == pytest.approx(3500.0)


def test_companies_without_fx_leaves_usd_none() -> None:
    ctx = StubContext(extra={"catalog_loader": lambda market: CATALOG.get(market, [])})
    obs = companies.collect([KR], TOP_COMPANIES, ctx)
    item = obs[0].payload["items"][0]
    assert item["market_cap_usd_bn"] is None
    assert item["market_cap_local"] == pytest.approx(525e12)
    assert companies.EXTRA_FX_RATES in ctx.log_text()


def test_companies_minor_unit_currency_conversion() -> None:
    assert companies.normalize_currency("GBp", 2.02e13) == ("GBP", 2.02e11)
    assert companies.normalize_currency("ZAc", 5.35e13) == ("ZAR", 5.35e11)
    assert companies.normalize_currency("GBP", 2.02e11) == ("GBP", 2.02e11)
    assert companies.normalize_currency(None, 1.0) == ("", 1.0)
    assert companies.normalize_currency("GBp", None) == ("GBP", None)


def test_companies_to_usd_bn() -> None:
    assert companies.to_usd_bn(1.38e12, "KRW", FX) == pytest.approx(1.0)
    assert companies.to_usd_bn(1e9, "USD", FX) == pytest.approx(1.0)
    assert companies.to_usd_bn(1e9, "XYZ", FX) is None
    assert companies.to_usd_bn(None, "KRW", FX) is None


def test_companies_yfinance_path_and_empty_ticker_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caps = {"SHEL.L": (2.02e13, "GBp"), "AZN.L": (1.9e13, "GBp"), "BOOM.L": (None, "GBp")}

    class FakeTicker:
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol

        @property
        def fast_info(self) -> dict[str, Any]:
            if self.symbol not in caps:
                raise RuntimeError("no data")
            cap, ccy = caps[self.symbol]
            return {"marketCap": cap, "currency": ccy}

        @property
        def info(self) -> dict[str, Any]:
            return {"shortName": f"{self.symbol} PLC", "sector": "Energy", "currency": "GBp"}

    monkeypatch.setitem(sys.modules, "yfinance", type("M", (), {"Ticker": FakeTicker}))
    monkeypatch.setitem(companies.INDEX_TICKERS, "GB", ("SHEL.L", "AZN.L", "BOOM.L", "GONE.L"))

    GB = FakeCountry(iso="GB", iso3="GBR", ccy="GBP", codes={"wb": "GBR"})
    RU = FakeCountry(iso="RU", iso3="RUS", ccy="RUB", codes={"wb": "RUS"})
    ctx = StubContext(extra={"fx_rates": {"GBP": 0.77}})
    obs = companies.collect([GB, RU], TOP_COMPANIES, ctx)

    gb = next(o for o in obs if o.iso == "GB")
    assert gb.source == "yahoo"
    assert gb.payload["source_detail"] == "yfinance"
    items = gb.payload["items"]
    assert [it["ticker"] for it in items] == ["SHEL.L", "AZN.L"]  # 조회 실패 종목 제외
    assert items[0]["currency"] == "GBP"  # 펜스 → 파운드
    assert items[0]["market_cap_local"] == pytest.approx(2.02e11)
    assert items[0]["market_cap_usd_bn"] == pytest.approx(2.02e11 / 0.77 / 1e9)
    assert items[0]["name"] == items[0]["label"] == "SHEL.L PLC"  # 상위 항목만 .info로 보강
    assert items[0]["sector"] == "Energy"

    # 러시아는 Yahoo가 시세를 주지 않아 티커 목록이 비어 있다 → 빈 결과 + 로그
    assert not any(o.iso == "RU" for o in obs)
    assert companies.NO_TICKER_NOTE in ctx.log_text()


def test_companies_catalog_loader_failure_is_isolated() -> None:
    def boom(market: str) -> list[dict[str, Any]]:
        raise RuntimeError("S3 down")

    ctx = StubContext(extra={"catalog_loader": boom, "fx_rates": FX})
    obs = companies.collect([KR], TOP_COMPANIES, ctx)
    assert obs == []
    assert any("companies:KR" in e and "S3 down" in e for e in ctx.errors)


def test_companies_current_quarter() -> None:
    assert companies.current_quarter(date(2026, 1, 31)) == "2026-Q1"
    assert companies.current_quarter(date(2026, 3, 31)) == "2026-Q1"
    assert companies.current_quarter(date(2026, 4, 1)) == "2026-Q2"
    assert companies.current_quarter(date(2026, 9, 20)) == "2026-Q3"
    assert companies.current_quarter(date(2026, 12, 31)) == "2026-Q4"


# ====================================================================== 공통 계약
@pytest.mark.parametrize(
    "module", [oecd, imf, ember, owid, wits, companies], ids=lambda m: m.SOURCE_NAME
)
def test_source_module_interface(module: Any) -> None:
    """CONTRACT 7장 인터페이스와 12장 케이던스."""
    assert isinstance(module.SOURCE_NAME, str) and module.SOURCE_NAME
    assert module.CADENCE in {"daily", "weekly", "monthly", "quarterly"}
    assert callable(module.collect)
    assert isinstance(module.SUPPORTED, tuple) and module.SUPPORTED


@pytest.mark.parametrize("module", [oecd, imf, ember, owid, wits, companies])
def test_collect_with_no_countries_is_noop(module: Any) -> None:
    """국가가 없으면 예외 없이 빈 리스트 (수집기 스모크)."""
    ctx = StubContext()
    assert module.collect([], [FakeIndicator(module.SUPPORTED[0])], ctx) == []
    assert ctx.errors == []


def test_cadences_match_contract_chapter_12() -> None:
    assert oecd.CADENCE == "weekly"
    assert imf.CADENCE == "weekly"
    assert ember.CADENCE == "monthly"
    assert owid.CADENCE == "monthly"
    assert wits.CADENCE == "monthly"
    assert companies.CADENCE == "quarterly"


def test_composite_indicators_use_payload_not_value() -> None:
    """복합값 3종은 value=None + payload (CONTRACT 2·3장)."""
    ctx = StubContext(
        {**EMBER_CSV_ROUTES, **WITS_ROUTES},
        since=date(2023, 1, 1),
        extra={"catalog_loader": lambda m: CATALOG.get(m, []), "fx_rates": FX},
    )
    obs: list[Observation] = []
    obs += ember.collect([KR], ELEC_MIX, ctx)
    obs += wits.collect([KR], [FakeIndicator("exports_top_hs2")], ctx)
    obs += companies.collect([KR], TOP_COMPANIES, ctx)

    got = {o.indicator for o in obs}
    assert got == {"elec_mix", "exports_top_hs2", "top_companies"}
    for ob in obs:
        assert ob.value is None
        assert isinstance(ob.payload, dict)
        assert ob.payload["items"], f"{ob.indicator} payload.items가 비어 있다"
        for item in ob.payload["items"]:
            assert "label" in item and "value" in item, f"{ob.indicator} item에 label/value 필요"
        # DynamoDB 직렬화(Decimal 변환)까지 통과하는지 확인
        item = ob.to_item()
        assert item["pk"] == f"OBS#{ob.indicator}#KR"
        assert "value" not in item or item["value"] is None
