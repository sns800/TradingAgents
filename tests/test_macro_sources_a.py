"""[모듈 개요] 매크로 정량 소스 A(bis·worldbank·yahoo·fred) 파서 테스트.

픽스처 `tests/fixtures/macro/*`는 실제 공개 API 응답(BIS SDMX v2 CSV, World Bank
Indicators JSON, Yahoo chart JSON)에서 잘라낸 고정본이고, FRED만 키가 없어 공식
문서 형식으로 손수 만들었습니다. 네트워크는 `ctx.http`를 응답 스텁으로 갈아끼워
차단하므로 이 테스트는 오프라인에서 결정적으로 돌아갑니다. 실제 API가 살아 있는지는
`scripts/macro_smoke.py`로 확인합니다.

검증 대상: Observation 생성(freq/period/unit/series_id), D→M 월말 집계, 유로
회원국 건너뛰기, 국가별 실패 격리(한 국가 500 → errors 1건 + 나머지 정상),
yahoo invert 처리, FRED 키 없음 → 빈 결과.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "webui"))

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "macro"

from macro.schema import Observation  # noqa: E402 - sys.path 설정 후 임포트
from macro.sources import bis, fred, worldbank, yahoo_fx  # noqa: E402
from macro.sources.base import (  # noqa: E402
    EURO_SHARED_INDICATORS,
    CollectContext,
    last_per_group,
    month_key,
    safe_float,
    sdmx_csv_to_rows,
)

pytestmark = pytest.mark.unit


# ====================================================================== 스텁
class Country:
    """registry.Country 최소 스텁 (base.py duck typing 계약)."""

    def __init__(self, iso, iso3=None, ccy=None, euro=False, codes=None):
        self.iso = iso
        self.iso3 = iso3
        self.ccy = ccy
        self.euro = euro
        self.codes = codes or {}


class Indicator:
    """registry.Indicator 최소 스텁."""

    def __init__(self, indicator_id, unit=None, sources=None):
        self.id = indicator_id
        self.unit = unit
        self.sources = sources or []

    def source_entries(self, name):
        return [e for e in self.sources if e.get("name") == name]


class Resp:
    """requests.Response 스텁 (status_code·text만)."""

    def __init__(self, text="", status_code=200):
        self.text = text
        self.status_code = status_code

    def json(self):
        import json

        return json.loads(self.text)


def make_ctx(route, since=None):
    """`route(url, params) -> Resp`를 쓰는 CollectContext를 만든다 (네트워크 차단).

    raw_saver는 no-op으로 두어 S3/로컬 쓰기를 모두 막는다.
    """
    http = MagicMock()
    http.get.side_effect = lambda url, params=None, headers=None, timeout=None: route(url, params)
    return CollectContext(
        since=since,
        dry_run=True,
        http=http,
        raw_saver=lambda source, name, data: None,
        log=lambda msg: None,
    )


def fixture(name):
    return (FIXTURES / name).read_text()


KR = Country("KR", "KOR", "KRW", False, {"bis": "KR", "wb": "KOR",
                                         "yahoo_fx": {"symbol": "KRW=X", "invert": False}})
US = Country("US", "USA", "USD", False, {"bis": "US", "wb": "USA"})
EU = Country("EU", "EA20", "EUR", False, {"bis": "XM", "wb": "EMU",
                                          "yahoo_fx": {"symbol": "EURUSD=X", "invert": True}})
DE = Country("DE", "DEU", "EUR", True, {"bis": "DE", "wb": "DEU",
                                        "yahoo_fx": {"symbol": "EURUSD=X", "invert": True}})


# ====================================================================== base
class TestBaseHelpers:
    def test_sdmx_csv_to_rows_handles_quoted_commas(self):
        rows = sdmx_csv_to_rows(fixture("bis_ws_cbpol_d.csv"))
        assert rows
        assert {"FREQ", "REF_AREA", "TIME_PERIOD", "OBS_VALUE"} <= set(rows[0])
        # XM의 COMPILATION에는 쉼표가 많아 수동 split이면 컬럼이 밀린다.
        xm = [r for r in rows if r["REF_AREA"] == "XM"]
        assert xm and all(r["TIME_PERIOD"].startswith("2026-") for r in xm)

    def test_sdmx_csv_to_rows_rejects_xml_error_body(self):
        body = '<?xml version="1.0" ?><message:Error><com:Text>No results</com:Text></message:Error>'
        assert sdmx_csv_to_rows(body) == []
        assert sdmx_csv_to_rows("") == []

    def test_month_key_and_safe_float(self):
        assert month_key("2026-08-28") == "2026-08"
        assert month_key("2026-08") == "2026-08"
        with pytest.raises(ValueError):
            month_key("20260828")
        assert safe_float("1,368.03") == pytest.approx(1368.03)
        assert safe_float(".") is None
        assert safe_float("") is None
        assert safe_float(None) is None

    def test_last_per_group_takes_month_end(self):
        pairs = [("2026-07-30", 2.75), ("2026-07-02", 2.5), ("2026-08-28", 3.0)]
        assert last_per_group(pairs) == [("2026-07", 2.75), ("2026-08", 3.0)]

    def test_euro_shared_indicator_set_matches_contract(self):
        expected = {"policy_rate", "m2_level", "m2_yoy", "fx_usd", "fx_value_index"}
        assert set(EURO_SHARED_INDICATORS) == expected

    def test_record_error_format(self):
        ctx = make_ctx(lambda url, params: Resp())
        ctx.record_error("bis", "KR", "WS_CBPOL HTTP 500")
        assert ctx.errors == ["bis:KR: WS_CBPOL HTTP 500"]


# ====================================================================== BIS
class TestBis:
    def test_policy_rate_daily_and_month_end(self):
        indicators = [Indicator("policy_rate", "%")]
        ctx = make_ctx(lambda url, params: Resp(fixture("bis_ws_cbpol_d.csv")),
                       since=date(2026, 5, 20))
        obs = bis.collect([KR, US, EU], indicators, ctx)

        assert ctx.errors == []
        assert all(isinstance(o, Observation) for o in obs)
        assert {o.source for o in obs} == {"bis"}
        # 픽스처에는 KR·XM만 있으므로 US는 조용히 건너뛴다(오류 아님).
        assert {o.iso for o in obs} == {"KR", "EU"}

        daily = [o for o in obs if o.freq == "D" and o.iso == "KR"]
        assert daily[-1].period == "2026-08-28"
        assert daily[-1].value == pytest.approx(3.0)
        assert daily[0].unit == "%"
        assert daily[0].series_id == "WS_CBPOL/1.0/D.KR"
        assert daily[0].method == "일별 정책금리 (BIS)"

        monthly = {o.period: o.value for o in obs if o.freq == "M" and o.iso == "KR"}
        # 2026-07은 월말(07-31) 값 2.75, 2026-08은 월말(08-28) 값 3.0
        assert monthly["2026-07"] == pytest.approx(2.75)
        assert monthly["2026-08"] == pytest.approx(3.0)
        assert monthly["2026-05"] == pytest.approx(2.5)
        assert [o for o in obs if o.freq == "M"][0].method == "월말 기준값 (원천: 일별)"

        # 유로존은 XM 코드로 매핑되고 iso는 EU로 저장된다.
        eu_monthly = {o.period: o.value for o in obs if o.freq == "M" and o.iso == "EU"}
        assert eu_monthly["2026-09"] == pytest.approx(2.25)
        assert [o for o in obs if o.iso == "EU"][0].series_id == "WS_CBPOL/1.0/D.XM"

    def test_malformed_time_period_rows_are_skipped_with_country_error(self):
        """TIME_PERIOD 형식이 어긋난 행은 건너뛰고(국가 단위 record_error 1건) 나머지 국가·행은 그대로."""
        header = fixture("bis_ws_cbpol_d.csv").splitlines()[0]
        cols = header.split(",")

        def row(area, period, value):
            cells = [""] * len(cols)
            cells[cols.index("FREQ")] = "D"
            cells[cols.index("REF_AREA")] = area
            cells[cols.index("TIME_PERIOD")] = period
            cells[cols.index("OBS_VALUE")] = str(value)
            return ",".join(cells)

        csv = "\n".join([
            header,
            row("KR", "2026-09-17", 2.5),
            row("KR", "2026-09", 2.5),        # 월 형식이 일별 플로우에 섞여 옴
            row("KR", "2026/09/18", 2.5),     # 구분자 오류
            row("XM", "2026-09-18", 2.25),    # 다른 국가는 정상
        ])
        indicators = [Indicator("policy_rate", "%")]
        ctx = make_ctx(lambda url, params: Resp(csv), since=date(2026, 9, 1))
        obs = bis.collect([KR, EU], indicators, ctx)
        kr_daily = sorted(o.period for o in obs if o.iso == "KR" and o.freq == "D")
        assert kr_daily == ["2026-09-17"]
        assert [o.period for o in obs if o.iso == "EU" and o.freq == "D"] == ["2026-09-18"]
        assert len(ctx.errors) == 1
        assert ctx.errors[0].startswith("bis:KR: WS_CBPOL TIME_PERIOD 형식 오류 2행")
        assert "YYYY-MM-DD" in ctx.errors[0] and "'2026-09'" in ctx.errors[0]

        # 순수 함수 단위: freq를 주지 않으면 검증하지 않는다 (기존 호출 호환)
        rows = [{"REF_AREA": "KR", "TIME_PERIOD": "2026-Q3", "OBS_VALUE": "1"}]
        assert bis._pairs(rows, "KR") == [("2026-Q3", 1.0)]
        assert bis._pairs(rows, "KR", freq="Q") == [("2026-Q3", 1.0)]
        assert bis._pairs(rows, "KR", freq="M") == []
        assert bis._pairs([{"REF_AREA": "KR", "TIME_PERIOD": "2026-Q5", "OBS_VALUE": "1"}], "KR", freq="Q") == []

    def test_euro_member_skipped_for_policy_rate(self):
        """DE는 euro=True → policy_rate 요청 키에 들어가지 않는다 (CONTRACT 1장)."""
        seen = {}

        def route(url, params):
            seen["url"] = url
            return Resp(fixture("bis_ws_cbpol_d.csv"))

        ctx = make_ctx(route)
        obs = bis.collect([KR, DE, EU], [Indicator("policy_rate", "%")], ctx)
        assert "DE" not in seen["url"].rsplit("/", 1)[-1]
        assert "DE" not in {o.iso for o in obs}

    def test_house_price_index_keeps_euro_member(self):
        """house_price_index는 유로 공통 지표가 아니므로 DE도 수집한다."""
        seen = {}

        def route(url, params):
            seen["url"] = url
            return Resp(fixture("bis_ws_spp_q.csv"))

        ctx = make_ctx(route)
        obs = bis.collect([KR, US, EU, DE], [Indicator("house_price_index", "index")], ctx)
        key = seen["url"].rsplit("/", 1)[-1]
        assert key.startswith("Q.") and key.endswith(".N.628")
        assert "DE" in key
        kr = [o for o in obs if o.iso == "KR"]
        assert kr and kr[0].freq == "Q"
        assert kr[-1].period.startswith("2026-Q") or kr[-1].period.startswith("2025-Q")
        assert kr[0].unit == "index"
        assert kr[0].series_id == "WS_SPP/1.0/Q.KR.N.628"

    def test_fx_usd_uses_local_currency_and_end_of_period(self):
        seen = {}

        def route(url, params):
            seen["url"] = url
            return Resp(fixture("bis_ws_xru_m.csv"))

        ctx = make_ctx(route)
        obs = bis.collect([KR, US, EU, DE], [Indicator("fx_usd", "lcu_per_usd")], ctx)
        key = seen["url"].rsplit("/", 1)[-1]
        # 3번째 차원은 USD가 아니라 현지통화이고, 마지막은 COLLECTION=E(기말)
        assert key.startswith("M.") and key.endswith(".E")
        assert "KRW" in key and "EUR" in key and "USD" not in key
        # US는 기준통화라 요청하지 않고, DE는 유로 참조로 제외된다.
        assert "DE" not in key.split(".")[1].split("+")
        isos = {o.iso for o in obs}
        assert isos == {"KR", "EU"}
        kr = [o for o in obs if o.iso == "KR"]
        assert kr[0].freq == "M"
        assert kr[-1].value > 1000  # 원/달러
        assert kr[0].unit == "lcu_per_usd"
        assert kr[0].series_id == "WS_XRU/1.0/M.KR.KRW.E"
        assert "기말" in kr[0].method
        eu = [o for o in obs if o.iso == "EU"]
        assert eu[-1].value < 2  # 유로/달러

    def test_http_500_is_isolated_per_flow(self):
        """한 지표(플로)가 500이어도 나머지 지표는 정상 수집된다."""

        def route(url, params):
            if "WS_CBPOL" in url:
                return Resp("upstream error", status_code=500)
            return Resp(fixture("bis_ws_spp_q.csv"))

        ctx = make_ctx(route)
        obs = bis.collect(
            [KR, US, EU],
            [Indicator("policy_rate", "%"), Indicator("house_price_index", "index")],
            ctx,
        )
        assert len(ctx.errors) == 1
        assert ctx.errors[0].startswith("bis:")
        assert "WS_CBPOL HTTP 500" in ctx.errors[0]
        assert {o.indicator for o in obs} == {"house_price_index"}
        assert {o.iso for o in obs} == {"KR", "US", "EU"}

    def test_404_no_results_is_not_an_error(self):
        """BIS는 '조회 구간에 관측 없음'도 404로 준다 → 오류로 세지 않는다."""
        body = (
            '<?xml version="1.0" ?><message:Error>'
            '<message:ErrorMessage code="100"><com:Text>No results for query</com:Text>'
            "</message:ErrorMessage></message:Error>"
        )
        ctx = make_ctx(lambda url, params: Resp(body, status_code=404))
        obs = bis.collect([KR], [Indicator("house_price_index", "index")], ctx)
        assert obs == []
        assert ctx.errors == []

    def test_since_uses_frequency_lookback(self):
        """발표 지연 때문에 since를 빈도별 룩백만큼 뒤로 밀어야 404를 피한다."""
        seen = {}

        flows = {
            "WS_SPP": "bis_ws_spp_q.csv",
            "WS_XRU": "bis_ws_xru_m.csv",
            "WS_CBPOL": "bis_ws_cbpol_d.csv",
        }

        def route(url, params):
            seen[params["startPeriod"]] = url
            name = next(v for k, v in flows.items() if k in url)
            return Resp(fixture(name))

        # Q: 2026-06-01 → 분기초(2026-04-01)에서 4분기 전 = 2025-04-01
        ctx = make_ctx(route, since=date(2026, 6, 1))
        bis.collect([KR], [Indicator("house_price_index", "index")], ctx)
        assert "2025-04-01" in seen

        # M: 2026-06-15 → 3개월 전 월초 = 2026-03-01
        seen.clear()
        ctx = make_ctx(route, since=date(2026, 6, 15))
        bis.collect([KR], [Indicator("fx_usd", "lcu_per_usd")], ctx)
        assert "2026-03-01" in seen

        # D: 2026-06-15 → 7일 전 = 2026-06-08
        seen.clear()
        ctx = make_ctx(route, since=date(2026, 6, 15))
        bis.collect([KR], [Indicator("policy_rate", "%")], ctx)
        assert "2026-06-08" in seen

    def test_no_since_uses_default_start(self):
        seen = {}

        def route(url, params):
            seen["params"] = params
            return Resp(fixture("bis_ws_cbpol_d.csv"))

        ctx = make_ctx(route)
        bis.collect([KR], [Indicator("policy_rate", "%")], ctx)
        assert seen["params"]["startPeriod"] == bis.DEFAULT_START


# ================================================================== worldbank
class TestWorldBank:
    def test_annual_observations_and_vintage(self):
        ctx = make_ctx(lambda url, params: Resp(fixture("worldbank_gdp_usd.json")))
        obs = worldbank.collect([KR, US, EU], [Indicator("gdp_usd", "usd")], ctx)

        assert ctx.errors == []
        assert {o.iso for o in obs} == {"KR", "US", "EU"}
        assert {o.freq for o in obs} == {"Y"}
        assert all(len(o.period) == 4 and o.period.isdigit() for o in obs)
        kr = sorted((o for o in obs if o.iso == "KR"), key=lambda o: o.period)
        assert kr[-1].value > 1e12
        assert kr[0].unit == "usd"
        assert kr[0].source == "worldbank"
        assert kr[0].series_id == "NY.GDP.MKTP.CD"
        # 응답 헤더의 lastupdated가 vintage가 된다.
        assert kr[0].vintage == "2026-07-13"
        # 유로존은 country.id가 "XC"이므로 countryiso3code(EMU)로 매핑해야 한다.
        assert [o for o in obs if o.iso == "EU"]

    def test_null_values_skipped(self):
        doc = (
            '[{"page":1,"pages":1,"per_page":2000,"total":2,"sourceid":"2",'
            '"lastupdated":"2026-07-13"},'
            '[{"countryiso3code":"KOR","date":"2025","value":null},'
            '{"countryiso3code":"KOR","date":"2024","value":1.5}]]'
        )
        ctx = make_ctx(lambda url, params: Resp(doc))
        obs = worldbank.collect([KR], [Indicator("gdp_growth", "%")], ctx)
        assert [(o.period, o.value) for o in obs] == [("2024", 1.5)]

    def test_url_joins_iso3_with_semicolon_and_uses_emu(self):
        seen = {}

        def route(url, params):
            seen["url"] = url
            return Resp(fixture("worldbank_gdp_usd.json"))

        ctx = make_ctx(route)
        worldbank.collect([KR, US, EU], [Indicator("gdp_usd", "usd")], ctx)
        assert "EMU;KOR;USA" in seen["url"]
        assert "/indicator/NY.GDP.MKTP.CD" in seen["url"]

    def test_registry_entry_overrides_code_and_unit(self):
        """m2_level의 WB 폴백은 잔액이 아니라 광의통화/GDP → unit 오버라이드."""
        seen = {}

        def route(url, params):
            seen["url"] = url
            return Resp(
                '[{"page":1,"pages":1,"per_page":2000,"total":1,"lastupdated":"2026-07-13"},'
                '[{"countryiso3code":"KOR","date":"2024","value":160.5}]]'
            )

        indicator = Indicator(
            "m2_level",
            "usd_bn",
            sources=[
                {
                    "name": "worldbank",
                    "series": "FM.LBL.BMNY.GD.ZS",
                    "freq": "Y",
                    "unit": "pct_gdp",
                }
            ],
        )
        ctx = make_ctx(route)
        obs = worldbank.collect([KR], [indicator], ctx)
        assert "FM.LBL.BMNY.GD.ZS" in seen["url"]
        assert obs[0].unit == "pct_gdp"

    def test_http_500_isolated_per_indicator(self):
        def route(url, params):
            if "NY.GDP.MKTP.CD" in url:
                return Resp("boom", status_code=500)
            return Resp(fixture("worldbank_gdp_usd.json"))

        ctx = make_ctx(route)
        obs = worldbank.collect(
            [KR, US, EU],
            [Indicator("gdp_usd", "usd"), Indicator("gni_usd", "usd")],
            ctx,
        )
        assert len(ctx.errors) == 1
        assert "NY.GDP.MKTP.CD HTTP 500" in ctx.errors[0]
        assert {o.indicator for o in obs} == {"gni_usd"}

    def test_pagination_follows_pages(self):
        calls = []

        def route(url, params):
            calls.append(params["page"])
            page = params["page"]
            body = (
                f'[{{"page":{page},"pages":2,"per_page":1,"total":2,'
                '"lastupdated":"2026-07-13"},'
                f'[{{"countryiso3code":"KOR","date":"202{page}","value":{page}.0}}]]'
            )
            return Resp(body)

        ctx = make_ctx(route)
        obs = worldbank.collect([KR], [Indicator("gdp_growth", "%")], ctx)
        assert calls == [1, 2]
        assert sorted(o.period for o in obs) == ["2021", "2022"]

    def test_message_error_body_recorded(self):
        ctx = make_ctx(
            lambda url, params: Resp('[{"message":[{"key":"Invalid value","value":"x"}]}]')
        )
        obs = worldbank.collect([KR], [Indicator("gdp_usd", "usd")], ctx)
        assert obs == []
        assert len(ctx.errors) == 1


# ====================================================================== yahoo
class TestYahooFx:
    def test_daily_close_without_invert(self):
        ctx = make_ctx(lambda url, params: Resp(fixture("yahoo_chart_krw.json")))
        obs = yahoo_fx.collect([KR], [Indicator("fx_usd", "lcu_per_usd")], ctx)

        assert ctx.errors == []
        assert {o.freq for o in obs} == {"D"}
        assert {o.iso for o in obs} == {"KR"}
        assert all(len(o.period) == 10 for o in obs)
        assert obs[0].source == "yahoo"
        assert obs[0].series_id == "KRW=X"
        assert obs[0].unit == "lcu_per_usd"
        assert obs[-1].value > 1000
        assert "역수" not in obs[0].method

    def test_invert_produces_reciprocal(self):
        raw = fixture("yahoo_chart_eurusd.json")
        ctx = make_ctx(lambda url, params: Resp(raw))
        obs = yahoo_fx.collect([EU], [Indicator("fx_usd", "lcu_per_usd")], ctx)

        import json

        node = json.loads(raw)["chart"]["result"][0]
        closes = [c for c in node["indicators"]["quote"][0]["close"] if c is not None]
        assert obs[-1].value == pytest.approx(1.0 / closes[-1])
        assert 0.5 < obs[-1].value < 2.0  # EUR/USD의 역수 범위
        assert "역수" in obs[-1].method

    def test_period_uses_exchange_local_date(self):
        """timestamp는 거래소 로컬 자정의 UTC epoch → gmtoffset 보정 필요."""
        doc = {
            "chart": {
                "result": [
                    {
                        "meta": {"gmtoffset": 3600, "symbol": "KRW=X"},
                        "timestamp": [1789340400, 1789426800],
                        "indicators": {"quote": [{"close": [1344.64, None]}]},
                    }
                ],
                "error": None,
            }
        }
        import json

        ctx = make_ctx(lambda url, params: Resp(json.dumps(doc)))
        obs = yahoo_fx.collect([KR], [Indicator("fx_usd", "lcu_per_usd")], ctx)
        # 1789340400 + 3600 = 2026-09-14T00:00Z (UTC로는 09-13T23:00)
        assert [o.period for o in obs] == ["2026-09-14"]
        assert obs[0].value == pytest.approx(1344.64)

    def test_duplicate_date_keeps_latest(self):
        """장중 실시간 시세로 같은 날짜가 두 번 오면 마지막 값을 쓴다."""
        doc = {
            "chart": {
                "result": [
                    {
                        "meta": {"gmtoffset": 0},
                        "timestamp": [1789344000, 1789380000],
                        "indicators": {"quote": [{"close": [1380.0, 1385.0]}]},
                    }
                ]
            }
        }
        rows = yahoo_fx.parse_chart(doc)
        assert rows == [("2026-09-14", 1385.0)]

    def test_dxy_stored_as_us_index(self):
        ctx = make_ctx(lambda url, params: Resp(fixture("yahoo_chart_dxy.json")))
        obs = yahoo_fx.collect([US], [Indicator("dxy", "index")], ctx)
        assert obs
        assert {o.iso for o in obs} == {"US"}
        assert {o.indicator for o in obs} == {"dxy"}
        assert obs[0].unit == "index"
        assert obs[0].series_id == "DX-Y.NYB"
        assert 50 < obs[-1].value < 200

    def test_euro_member_skipped(self):
        ctx = make_ctx(lambda url, params: Resp(fixture("yahoo_chart_eurusd.json")))
        obs = yahoo_fx.collect([DE], [Indicator("fx_usd", "lcu_per_usd")], ctx)
        assert obs == []
        assert ctx.errors == []

    def test_us_skipped_for_fx(self):
        ctx = make_ctx(lambda url, params: Resp(fixture("yahoo_chart_krw.json")))
        obs = yahoo_fx.collect([US], [Indicator("fx_usd", "lcu_per_usd")], ctx)
        assert obs == []

    def test_failure_isolated_per_symbol(self, monkeypatch):
        """KR가 500이어도 EU는 정상 수집되고 오류는 1건만 기록된다."""

        def route(url, params):
            if "KRW=X" in url:
                return Resp("rate limited", status_code=500)
            return Resp(fixture("yahoo_chart_eurusd.json"))

        ctx = make_ctx(route)
        # yfinance 폴백은 네트워크를 타므로 비활성화한다.
        monkeypatch.setattr(yahoo_fx, "_yfinance_fallback", lambda *a, **k: [])
        obs = yahoo_fx.collect([KR, EU], [Indicator("fx_usd", "lcu_per_usd")], ctx)
        assert len(ctx.errors) == 1
        assert ctx.errors[0].startswith("yahoo:KR: ")
        assert {o.iso for o in obs} == {"EU"}

    def test_range_maps_since_to_yahoo_label(self):
        assert yahoo_fx._range(None) == "10y"
        assert yahoo_fx._range(date.today()) == "5d"

    def test_no_monthly_observations(self):
        """이 모듈은 freq D만 저장한다 (월별 fx_usd는 BIS가 1차 소스)."""
        ctx = make_ctx(lambda url, params: Resp(fixture("yahoo_chart_krw.json")))
        obs = yahoo_fx.collect([KR], [Indicator("fx_usd", "lcu_per_usd")], ctx)
        assert {o.freq for o in obs} == {"D"}


# ====================================================================== FRED
class TestFred:
    def test_no_api_key_returns_empty(self, monkeypatch):
        monkeypatch.delenv("FRED_API_KEY", raising=False)
        logs = []
        ctx = make_ctx(lambda url, params: Resp("{}"))
        ctx.log = logs.append
        obs = fred.collect([US], [Indicator("policy_rate", "%")], ctx)
        assert obs == []
        assert ctx.errors == []
        assert len(logs) == 1 and "FRED_API_KEY" in logs[0]

    def test_blank_api_key_returns_empty(self, monkeypatch):
        monkeypatch.setenv("FRED_API_KEY", "   ")
        ctx = make_ctx(lambda url, params: Resp("{}"))
        assert fred.collect([US], [Indicator("policy_rate", "%")], ctx) == []

    def test_monthly_observations(self, monkeypatch):
        monkeypatch.setenv("FRED_API_KEY", "testkey")
        seen = {}

        def route(url, params):
            seen["params"] = params
            return Resp(fixture("fred_fedfunds.json"))

        ctx = make_ctx(route, since=date(2026, 1, 1))
        obs = fred.collect([US, KR], [Indicator("policy_rate", "%")], ctx)

        assert ctx.errors == []
        assert seen["params"]["series_id"] == "FEDFUNDS"
        assert seen["params"]["file_type"] == "json"
        assert seen["params"]["observation_start"] == "2026-01-01"
        assert {o.iso for o in obs} == {"US"}
        assert {o.freq for o in obs} == {"M"}
        # "." 결측(2026-08)은 건너뛴다.
        assert [o.period for o in obs] == [
            "2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06", "2026-07",
        ]
        assert obs[-1].value == pytest.approx(3.625)
        assert obs[0].unit == "%"
        assert obs[0].series_id == "FEDFUNDS"
        assert obs[0].source == "fred"
        assert obs[0].vintage == "2026-09-20"
        # 소스 우선순위는 수집기가 판단하므로 fallback_source를 붙이지 않는다.
        assert obs[0].flags == []

    def test_daily_series_keeps_date_period(self, monkeypatch):
        monkeypatch.setenv("FRED_API_KEY", "testkey")
        body = (
            '{"realtime_start":"2026-09-20","observations":['
            '{"date":"2026-09-17","value":"120.5"},'
            '{"date":"2026-09-18","value":"."}]}'
        )
        ctx = make_ctx(lambda url, params: Resp(body))
        obs = fred.collect([US], [Indicator("dxy", "index")], ctx)
        assert [(o.freq, o.period, o.value) for o in obs] == [("D", "2026-09-17", 120.5)]
        assert obs[0].series_id == "DTWEXBGS"

    def test_us_only(self, monkeypatch):
        monkeypatch.setenv("FRED_API_KEY", "testkey")
        ctx = make_ctx(lambda url, params: Resp(fixture("fred_fedfunds.json")))
        assert fred.collect([KR, EU], [Indicator("policy_rate", "%")], ctx) == []

    def test_http_400_recorded_with_message(self, monkeypatch):
        monkeypatch.setenv("FRED_API_KEY", "badkey")
        body = '{"error_code":400,"error_message":"api_key is not registered."}'
        ctx = make_ctx(lambda url, params: Resp(body, status_code=400))
        obs = fred.collect([US], [Indicator("policy_rate", "%")], ctx)
        assert obs == []
        assert len(ctx.errors) == 1
        assert "HTTP 400" in ctx.errors[0] and "api_key" in ctx.errors[0]

    def test_isolated_failure_between_series(self, monkeypatch):
        monkeypatch.setenv("FRED_API_KEY", "testkey")

        def route(url, params):
            if params["series_id"] == "FEDFUNDS":
                return Resp("boom", status_code=500)
            return Resp(fixture("fred_fedfunds.json"))

        ctx = make_ctx(route)
        obs = fred.collect(
            [US], [Indicator("policy_rate", "%"), Indicator("cpi_index", "index")], ctx
        )
        assert len(ctx.errors) == 1
        assert {o.indicator for o in obs} == {"cpi_index"}

    def test_template_series_skipped(self, monkeypatch):
        """registry의 `{fred_fx}` 치환 템플릿은 US 전용 모듈이 풀 수 없어 건너뛴다."""
        monkeypatch.setenv("FRED_API_KEY", "testkey")
        indicator = Indicator(
            "fx_usd",
            "lcu_per_usd",
            sources=[{"name": "fred", "series": "{fred_fx}", "freq": "D"}],
        )
        ctx = make_ctx(lambda url, params: Resp(fixture("fred_fedfunds.json")))
        assert fred.collect([US], [indicator], ctx) == []

    def test_parse_observations_ignores_sentinel_vintage(self):
        rows, vintage = fred.parse_observations(
            {"realtime_start": "9999-12-31", "observations": [{"date": "2026-01-01",
                                                               "value": "1.0"}]}
        )
        assert rows == [("2026-01-01", 1.0)]
        assert vintage is None


# ================================================================ 모듈 계약
class TestModuleContract:
    @pytest.mark.parametrize(
        ("module", "name", "cadence"),
        [
            (bis, "bis", "daily"),
            (worldbank, "worldbank", "monthly"),
            (yahoo_fx, "yahoo", "daily"),
            (fred, "fred", "weekly"),
        ],
    )
    def test_source_name_and_cadence(self, module, name, cadence):
        assert name == module.SOURCE_NAME
        assert cadence == module.CADENCE
        assert callable(module.collect)

    @pytest.mark.parametrize("module", [bis, worldbank, yahoo_fx, fred])
    def test_unknown_indicator_is_ignored(self, module):
        ctx = make_ctx(lambda url, params: Resp("", status_code=500))
        assert module.collect([KR], [Indicator("party_support", "pct_share")], ctx) == []
        assert ctx.errors == []

    @pytest.mark.parametrize("module", [bis, worldbank, yahoo_fx, fred])
    def test_empty_inputs_are_safe(self, module):
        ctx = make_ctx(lambda url, params: Resp("", status_code=500))
        assert module.collect([], [], ctx) == []
