"""[모듈 개요] 데이터 공급자(vendor)의 빈 결과가 조작된 데이터로 둔갑하지 않는지 검증하는 테스트.

두 가지 체계적인 수정 사항을 다룬다:
  - load_ohlcv는 빈 다운로드 결과를 캐시하면 안 되고(캐시 오염(cache poisoning) 방지),
    빈 데이터프레임을 반환하는 대신 NoMarketDataError를 발생시켜야 한다.
  - route_to_vendor는 모든 공급자를 소진한 뒤 NoMarketDataError를 단일하고 명시적인
    "NO_DATA_AVAILABLE" 센티널(sentinel)로 변환해야 한다.
"""

import os
import unittest
from unittest import mock

import pandas as pd
import pytest

from tradingagents.dataflows import interface, stockstats_utils
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.symbol_utils import NoMarketDataError


@pytest.mark.unit
class TestLoadOhlcvNoPoison(unittest.TestCase):
    def setUp(self):
        self._tmp = os.path.join(os.path.dirname(__file__), "_tmp_cache")
        os.makedirs(self._tmp, exist_ok=True)
        set_config({"data_cache_dir": self._tmp})

    def tearDown(self):
        for f in os.listdir(self._tmp):
            os.remove(os.path.join(self._tmp, f))
        os.rmdir(self._tmp)

    def test_empty_download_raises_and_does_not_cache(self):
        """빈 다운로드 결과가 예외를 일으키고 캐시에 기록되지 않는지 검증하는 테스트."""
        empty = pd.DataFrame()
        with mock.patch.object(stockstats_utils.yf, "download", return_value=empty), \
                self.assertRaises(NoMarketDataError):
            stockstats_utils.load_ohlcv("FAKE", "2026-01-01")
        # 캐시에 아무것도 기록되지 않았어야 한다.
        self.assertEqual(os.listdir(self._tmp), [])

        # 두 번째 호출은 다시 가져오기를 시도해야 한다 (오염된 캐시를 제공하지 않음).
        with mock.patch.object(stockstats_utils.yf, "download", return_value=empty) as dl2:
            with self.assertRaises(NoMarketDataError):
                stockstats_utils.load_ohlcv("FAKE", "2026-01-01")
            self.assertTrue(dl2.called)


@pytest.mark.unit
class TestRouteToVendorSentinel(unittest.TestCase):
    def test_no_data_from_all_vendors_returns_sentinel(self):
        """모든 공급자가 데이터 없음일 때 NO_DATA_AVAILABLE 센티널을 반환하는지 검증하는 테스트."""
        def raises_no_data(symbol, *a, **k):
            raise NoMarketDataError(symbol, "GC=F", "no rows")

        patched = {"yfinance": raises_no_data, "alpha_vantage": raises_no_data}
        with mock.patch.dict(
            interface.VENDOR_METHODS, {"get_stock_data": patched}, clear=False
        ):
            result = interface.route_to_vendor(
                "get_stock_data", "XAUUSD+", "2026-01-01", "2026-01-10"
            )
        self.assertIn("NO_DATA_AVAILABLE", result)
        self.assertIn("XAUUSD+", result)
        self.assertIn("GC=F", result)
        self.assertIn("Do not estimate", result)

    def test_unconfigured_fallback_does_not_mask_no_data(self):
        """설정되지 않은 폴백(fallback) 공급자의 오류가 데이터 없음 신호를 가리지 않는지 검증하는 테스트."""
        # 기본(primary) 공급자가 데이터 없음을 보고하고 폴백은 단순히 사용 불가한
        # 상황(예: API 키 누락 -> 예외 발생)에서는, 폴백의 부수적인 오류로
        # 크래시가 나는 대신 데이터 없음 센티널이 우선해야 한다.
        def raises_no_data(symbol, *a, **k):
            raise NoMarketDataError(symbol, symbol, "no rows")

        def raises_unavailable(symbol, *a, **k):
            raise ValueError("ALPHA_VANTAGE_API_KEY environment variable is not set.")

        patched = {"yfinance": raises_no_data, "alpha_vantage": raises_unavailable}
        with mock.patch.dict(
            interface.VENDOR_METHODS, {"get_stock_data": patched}, clear=False
        ):
            result = interface.route_to_vendor(
                "get_stock_data", "FAKE", "2026-01-01", "2026-01-10"
            )
        self.assertIn("NO_DATA_AVAILABLE", result)


if __name__ == "__main__":
    unittest.main()
