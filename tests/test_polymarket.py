"""[모듈 개요] 폴리마켓(Polymarket) 예측 시장(prediction market) 데이터 공급자를
검증하는 테스트: 미래 지향 필터링, 거래량(volume) 순위 정렬, 출력 형식,
장애 시 점진적 성능 저하(graceful degradation), 라우터 통합을 다룬다.

모든 API 접근은 모의(mock) 처리되어 네트워크 연결 없이 실행된다.
"""
import copy
import unittest
from unittest import mock

import pytest
import requests

import tradingagents.dataflows.config as config_module
import tradingagents.default_config as default_config
from tradingagents.dataflows import interface, polymarket
from tradingagents.dataflows.config import set_config


def _market(question, prob, *, volume, end_date, closed=False, wk=None):
    return {
        "question": question,
        "outcomes": '["Yes", "No"]',
        "outcomePrices": f'["{prob}", "{round(1 - prob, 4)}"]',
        "volumeNum": volume,
        "endDate": end_date,
        "closed": closed,
        "oneWeekPriceChange": wk,
    }


# 여러 유형이 섞인 이벤트 하나: 거래량 많은 열린 시장, 종료(closed)된 시장,
# 날짜가 지난 시장, 거래량 적은 열린 시장. 먼 미래/먼 과거 날짜를 써서
# 테스트가 실제 시계에 의존하지 않게 한다.
_SEARCH = {
    "events": [
        {
            "markets": [
                _market("Open big?", 0.76, volume=5_000_000, end_date="2030-12-31T00:00:00Z", wk=-0.045),
                _market("Resolved already?", 1.0, volume=9_000_000, end_date="2030-12-31T00:00:00Z", closed=True),
                _market("Past event?", 0.5, volume=8_000_000, end_date="2020-01-01T00:00:00Z"),
                _market("Open small?", 0.30, volume=1_000, end_date="2030-06-30T00:00:00Z"),
            ]
        }
    ]
}


@pytest.mark.unit
class PolymarketFilterTests(unittest.TestCase):
    def test_closed_and_past_markets_are_excluded(self):
        """종료된 시장과 마감일이 지난 시장이 결과에서 제외되는지 검증하는 테스트."""
        with mock.patch.object(polymarket, "_request", return_value=_SEARCH):
            out = polymarket.get_prediction_markets("anything", limit=10)
        self.assertIn("Open big?", out)
        self.assertIn("Open small?", out)
        self.assertNotIn("Resolved already?", out)  # 종료됨(closed)
        self.assertNotIn("Past event?", out)         # endDate가 과거

    def test_ranked_by_volume(self):
        """시장이 거래량(volume) 기준 내림차순으로 정렬되는지 검증하는 테스트."""
        with mock.patch.object(polymarket, "_request", return_value=_SEARCH):
            out = polymarket.get_prediction_markets("anything", limit=10)
        self.assertLess(out.index("Open big?"), out.index("Open small?"))

    def test_limit_caps_results(self):
        """limit 인자가 결과 개수를 제한하는지 검증하는 테스트."""
        with mock.patch.object(polymarket, "_request", return_value=_SEARCH):
            out = polymarket.get_prediction_markets("anything", limit=1)
        self.assertIn("Open big?", out)
        self.assertNotIn("Open small?", out)


@pytest.mark.unit
class PolymarketFormatTests(unittest.TestCase):
    def test_probability_volume_and_weekly_change_render(self):
        """확률, 거래량, 주간 변동이 올바른 형식으로 출력되는지 검증하는 테스트."""
        with mock.patch.object(polymarket, "_request", return_value=_SEARCH):
            out = polymarket.get_prediction_markets("anything", limit=10)
        self.assertIn("Yes 76%", out)
        self.assertIn("$5,000,000 volume", out)
        self.assertIn("resolves 2030-12-31", out)
        self.assertIn("1-week -4.5pp", out)  # -0.045 -> -4.5pp

    def test_weekly_change_omitted_when_absent(self):
        """주간 변동 데이터가 없으면 해당 문구가 생략되는지 검증하는 테스트."""
        # "Open small?"은 wk=None -> 해당 줄에 1-week 문구가 없어야 한다.
        with mock.patch.object(polymarket, "_request", return_value=_SEARCH):
            out = polymarket.get_prediction_markets("anything", limit=10)
        small_line = next(ln for ln in out.splitlines() if "Open small?" in ln)
        self.assertNotIn("1-week", small_line)

    def test_no_matches_reports_clearly(self):
        """일치하는 시장이 없을 때 명확한 안내 문구를 반환하는지 검증하는 테스트."""
        with mock.patch.object(polymarket, "_request", return_value={"events": []}):
            out = polymarket.get_prediction_markets("obscure ticker", limit=6)
        self.assertIn("No open prediction markets", out)


@pytest.mark.unit
class PolymarketResilienceTests(unittest.TestCase):
    def test_network_error_degrades_gracefully(self):
        """네트워크 오류 시 예외 대신 안내 문구로 점진적으로 대응하는지 검증하는 테스트."""
        # 외부 서비스 장애가 분석가(analyst)까지 예외로 전파되면 안 된다.
        with mock.patch.object(
            polymarket, "_request", side_effect=requests.RequestException("boom")
        ):
            out = polymarket.get_prediction_markets("Fed rate cut")
        self.assertIn("unavailable", out.lower())
        self.assertIn("Fed rate cut", out)


@pytest.mark.unit
class PolymarketRoutingTests(unittest.TestCase):
    def setUp(self):
        config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)

    def tearDown(self):
        config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)

    def test_category_routes_to_polymarket(self):
        """prediction_markets 카테고리가 폴리마켓 공급자로 라우팅되는지 검증하는 테스트."""
        self.assertEqual(
            interface.get_category_for_method("get_prediction_markets"),
            "prediction_markets",
        )
        set_config({"data_vendors": {"prediction_markets": "polymarket"}})
        with mock.patch.dict(
            interface.VENDOR_METHODS,
            {"get_prediction_markets": {"polymarket": lambda *a, **k: "POLY_OK"}},
            clear=False,
        ):
            out = interface.route_to_vendor("get_prediction_markets", "fed", 5)
        self.assertEqual(out, "POLY_OK")


if __name__ == "__main__":
    unittest.main()
