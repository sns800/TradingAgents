"""[모듈 개요] 데이터 공급자(vendor) 오류 계층 구조를 검증하는 테스트.

"공급자가 쓸 만한 데이터를 반환하지 못함"에 해당하는 모든 상황은 VendorError에서
파생되므로, 라우터는 기반 타입만 잡으면 되고 새 공급자도 추가 처리 없이
끼워 넣을 수 있다.
"""
import copy
import unittest
from unittest import mock

import pytest

import tradingagents.dataflows.config as config_module
import tradingagents.default_config as default_config
from tradingagents.dataflows import interface
from tradingagents.dataflows.alpha_vantage_common import (
    AlphaVantageNotConfiguredError,
    AlphaVantageRateLimitError,
)
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.errors import (
    NoMarketDataError,
    VendorError,
    VendorNotConfiguredError,
    VendorRateLimitError,
)
from tradingagents.dataflows.fred import FredNotConfiguredError


@pytest.mark.unit
class HierarchyTests(unittest.TestCase):
    def test_all_conditions_derive_from_vendor_error(self):
        """모든 데이터 오류 클래스가 VendorError에서 파생되는지 검증하는 테스트."""
        for cls in (NoMarketDataError, VendorRateLimitError, VendorNotConfiguredError):
            self.assertTrue(issubclass(cls, VendorError))

    def test_not_configured_is_still_a_value_error(self):
        """미설정 오류가 여전히 ValueError이기도 한지 검증하는 테스트."""
        # 하위 호환(back-compat): 기존의 `except ValueError` 호출자가 계속 동작한다.
        self.assertTrue(issubclass(VendorNotConfiguredError, ValueError))

    def test_vendor_named_errors_subclass_the_generic_bases(self):
        """공급자 이름이 붙은 오류들이 범용 기반 클래스를 상속하는지 검증하는 테스트."""
        self.assertTrue(issubclass(AlphaVantageRateLimitError, VendorRateLimitError))
        self.assertTrue(issubclass(AlphaVantageNotConfiguredError, VendorNotConfiguredError))
        self.assertTrue(issubclass(FredNotConfiguredError, VendorNotConfiguredError))
        # ... 따라서 여전히 ValueError이기도 하다
        self.assertTrue(issubclass(FredNotConfiguredError, ValueError))

    def test_symbol_utils_reexports_no_market_data_error(self):
        """symbol_utils가 NoMarketDataError를 동일 객체로 재수출(re-export)하는지 검증하는 테스트."""
        from tradingagents.dataflows.symbol_utils import (
            NoMarketDataError as ReExported,
        )
        self.assertIs(ReExported, NoMarketDataError)


@pytest.mark.unit
class RouterHandlesBaseTypesTests(unittest.TestCase):
    def setUp(self):
        config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)

    def tearDown(self):
        config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)

    def test_rate_limit_subclass_caught_by_base(self):
        """요청 한도(rate-limit) 오류가 기반 타입으로 잡혀 다음 공급자로 넘어가는지 검증하는 테스트."""
        # 공급자 이름이 붙은 요청 한도 오류는 체인의 다음 공급자로 건너뛴다.
        set_config({"data_vendors": {"core_stock_apis": "alpha_vantage,yfinance"}})

        def _throttled(*a, **k):
            raise AlphaVantageRateLimitError("slow down")

        with mock.patch.dict(
            interface.VENDOR_METHODS,
            {"get_stock_data": {"alpha_vantage": _throttled, "yfinance": lambda *a, **k: "YF"}},
            clear=False,
        ):
            out = interface.route_to_vendor("get_stock_data", "AAPL", "2026-01-01", "2026-01-10")
        self.assertEqual(out, "YF")

    def test_not_configured_falls_through_to_next_vendor(self):
        """미설정 공급자를 건너뛰고 다음 공급자를 사용하는지 검증하는 테스트."""
        set_config({"data_vendors": {"core_stock_apis": "alpha_vantage,yfinance"}})

        def _unconfigured(*a, **k):
            raise AlphaVantageNotConfiguredError("no key")

        with mock.patch.dict(
            interface.VENDOR_METHODS,
            {"get_stock_data": {"alpha_vantage": _unconfigured, "yfinance": lambda *a, **k: "YF"}},
            clear=False,
        ):
            out = interface.route_to_vendor("get_stock_data", "AAPL", "2026-01-01", "2026-01-10")
        self.assertEqual(out, "YF")

    def test_sole_unconfigured_vendor_surfaces_the_error(self):
        """유일한 공급자가 미설정이면 오류가 겉으로 드러나는지 검증하는 테스트."""
        # 폴백이 없으면 미설정 상태가 사라지지 않고 드러나야 한다.
        set_config({"data_vendors": {"core_stock_apis": "alpha_vantage"}})

        def _unconfigured(*a, **k):
            raise AlphaVantageNotConfiguredError("no key")

        with mock.patch.dict(
            interface.VENDOR_METHODS,
            {"get_stock_data": {"alpha_vantage": _unconfigured}},
            clear=False,
        ), self.assertRaises(AlphaVantageNotConfiguredError):
            interface.route_to_vendor("get_stock_data", "AAPL", "2026-01-01", "2026-01-10")


if __name__ == "__main__":
    unittest.main()
