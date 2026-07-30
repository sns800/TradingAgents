"""[모듈 개요] 데이터 공급자(vendor) 라우터가 설정된 체인을 존중하고, 고장 난
기본(primary) 공급자를 조용히 숨기지 않는지 검증하는 테스트.

회귀(regression) 방지 대상: #988 (단일 공급자를 명시해도 다른 공급자로
폴백함), #289 (선택하지 않은 공급자에 대해 폴백이 실행됨), #989 (기본
공급자의 심각한 실패가 흔적 없이 삼켜짐).
"""
import copy
import unittest
from unittest import mock

import pytest

import tradingagents.dataflows.config as config_module
import tradingagents.default_config as default_config
from tradingagents.dataflows import interface
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.symbol_utils import NoMarketDataError


def _reset_config():
    # 강제 초기화: set_config()는 병합(merge)하므로 비어 있는 DEFAULT 딕셔너리
    # (예: tool_vendors)가 다른 테스트에서 새어 들어온 키를 지우지 못한다.
    # 전역 설정을 통째로 교체한다.
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)


def _no_data(symbol, *a, **k):
    raise NoMarketDataError(symbol, symbol, "no rows")


def _returns(value):
    def impl(symbol, *a, **k):
        return value
    return impl


def _raises(exc):
    def impl(symbol, *a, **k):
        raise exc
    return impl


@pytest.mark.unit
class VendorRoutingTests(unittest.TestCase):
    def setUp(self):
        _reset_config()

    def tearDown(self):
        _reset_config()

    def _route(self, vendors_for_get_stock_data):
        return mock.patch.dict(
            interface.VENDOR_METHODS,
            {"get_stock_data": vendors_for_get_stock_data},
            clear=False,
        )

    def test_explicit_single_vendor_does_not_fall_back(self):
        """단일 공급자를 명시하면 다른 공급자로 폴백하지 않는지 검증하는 테스트."""
        # #988: yfinance로 고정했으면 멀쩡한 alpha_vantage라도 사용하면 안 된다.
        set_config({"data_vendors": {"core_stock_apis": "yfinance"}})
        av = mock.Mock(side_effect=_returns("AV_DATA"))
        with self._route({"yfinance": _no_data, "alpha_vantage": av}):
            result = interface.route_to_vendor("get_stock_data", "FAKE", "2026-01-01", "2026-01-10")
        self.assertIn("NO_DATA_AVAILABLE", result)
        av.assert_not_called()  # 선택하지 않은 공급자는 한 번도 시도되지 않음

    def test_explicit_multi_vendor_falls_back_within_chain(self):
        """여러 공급자를 나열하면 체인 안에서 순서대로 폴백하는지 검증하는 테스트."""
        # 두 공급자를 모두 나열하면 순서 있는 폴백을 명시적으로 선택한 것이다.
        set_config({"data_vendors": {"core_stock_apis": "yfinance,alpha_vantage"}})
        with self._route({"yfinance": _no_data, "alpha_vantage": _returns("AV_DATA")}):
            result = interface.route_to_vendor("get_stock_data", "AAPL", "2026-01-01", "2026-01-10")
        self.assertEqual(result, "AV_DATA")

    def test_primary_error_is_logged_not_masked(self):
        """기본 공급자의 오류가 가려지지 않고 로그에 남는지 검증하는 테스트."""
        # #989: 기본 공급자 오류 + 폴백 데이터 없음 -> NO_DATA이지만, 실패는
        # 로그에서 보여야 한다 (고장 난 기본 공급자를 숨기지 않음).
        set_config({"data_vendors": {"core_stock_apis": "yfinance,alpha_vantage"}})
        with self._route({"yfinance": _raises(ValueError("boom")), "alpha_vantage": _no_data}), \
                self.assertLogs("tradingagents.dataflows.interface", level="WARNING") as cm:
            result = interface.route_to_vendor("get_stock_data", "AAPL", "2026-01-01", "2026-01-10")
        self.assertIn("NO_DATA_AVAILABLE", result)
        joined = "\n".join(cm.output)
        self.assertIn("boom", joined)            # 실제 오류가 로그에 드러남
        self.assertIn("yfinance", joined)

    def test_unknown_configured_vendor_raises(self):
        """알 수 없는 공급자 이름을 설정하면 명확한 오류가 발생하는지 검증하는 테스트."""
        set_config({"data_vendors": {"core_stock_apis": "bogus_vendor"}})
        with self.assertRaises(ValueError) as ctx:
            interface.route_to_vendor("get_stock_data", "AAPL", "2026-01-01", "2026-01-10")
        self.assertIn("bogus_vendor", str(ctx.exception))

    def test_default_sentinel_uses_all_vendors(self):
        """"default" 설정이 전체 공급자 체인을 사용하는지 검증하는 테스트."""
        # 명시적 선택이 없으면("default") 회복력 있는 전체 체인 동작을 유지한다.
        set_config({"data_vendors": {"core_stock_apis": "default"}})
        with self._route({"yfinance": _no_data, "alpha_vantage": _returns("AV_DATA")}):
            result = interface.route_to_vendor("get_stock_data", "AAPL", "2026-01-01", "2026-01-10")
        self.assertEqual(result, "AV_DATA")

    def _route_method(self, method, vendors):
        return mock.patch.dict(interface.VENDOR_METHODS, {method: vendors}, clear=False)

    def test_optional_category_degrades_instead_of_raising(self):
        """선택적(optional) 카테고리는 오류 시 실행 중단 대신 센티널로 대응하는지 검증하는 테스트."""
        # 선택적 보강용 공급자(FRED 매크로)가 예외를 던져도 실행을 중단하면
        # 안 된다 — 라우터가 센티널을 반환해 분석이 계속 진행되게 한다.
        set_config({"data_vendors": {"macro_data": "fred"}})
        with self._route_method(
            "get_macro_indicators", {"fred": _raises(ValueError("FRED 400: bad series"))}
        ):
            result = interface.route_to_vendor("get_macro_indicators", "cpi", "2026-01-01")
        self.assertIn("DATA_UNAVAILABLE", result)
        self.assertIn("macro_data", result)

    def test_core_category_still_raises_on_error(self):
        """핵심(core) 카테고리는 오류를 그대로 전파하는지 검증하는 테스트."""
        # 핵심 카테고리(공급자 하나만 설정)는 오류를 전파해 고장 난 기본 공급자가
        # 조용히 성능 저하되지 않고 크게 드러나게 한다.
        set_config({"data_vendors": {"core_stock_apis": "yfinance"}})
        with self._route({"yfinance": _raises(ValueError("boom"))}), \
                self.assertRaises(ValueError):
            interface.route_to_vendor("get_stock_data", "AAPL", "2026-01-01", "2026-01-10")


if __name__ == "__main__":
    unittest.main()
