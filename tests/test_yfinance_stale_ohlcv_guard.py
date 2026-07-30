"""[모듈 개요] 오래된(stale) OHLCV 보호 장치(guard)를 검증하는 테스트 (#1021).

공급자(vendor)가 1년 지난 부분 데이터프레임을 반환하면 최신 데이터인 것처럼
보고서에 들어가지 않고 거부되어야 한다.

보호 장치는 오래됨(stale)에 특화된 상세 메시지와 함께 NoMarketDataError를
발생시키므로, 라우터의 기존 다음-공급자-시도 + 단일 센티널(sentinel) 처리가
그대로 적용되고 센티널에 그 사유가 드러난다.
"""
import copy
import unittest
from unittest import mock

import pandas as pd
import pytest

import tradingagents.dataflows.config as config_module
import tradingagents.dataflows.y_finance as y_finance
import tradingagents.default_config as default_config
from tradingagents.dataflows import interface
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.stockstats_utils import _assert_ohlcv_not_stale
from tradingagents.dataflows.symbol_utils import NoMarketDataError


def _frame(date):
    return pd.DataFrame(
        {
            "Date": [pd.Timestamp(date)],
            "Open": [330.0],
            "High": [332.0],
            "Low": [328.0],
            "Close": [330.58],
            "Volume": [1_000_000],
        }
    )


@pytest.mark.unit
class StaleGuardUnitTests(unittest.TestCase):
    def test_recent_prior_trading_day_is_accepted(self):
        """직전 거래일 데이터는 신선한 것으로 허용되는지 검증하는 테스트."""
        # curr_date 하루 전 — 신선도(freshness) 허용 범위 안에 충분히 든다.
        _assert_ohlcv_not_stale(_frame("2026-06-10"), "2026-06-11", "CB")

    def test_year_old_row_is_rejected_with_detail(self):
        """1년 지난 데이터가 상세 사유와 함께 거부되는지 검증하는 테스트."""
        with self.assertRaises(NoMarketDataError) as ctx:
            _assert_ohlcv_not_stale(_frame("2025-06-11"), "2026-06-11", "CB", "CB")
        msg = str(ctx.exception)
        self.assertIn("2025-06-11", msg)
        self.assertIn("2026-06-11", msg)
        self.assertIn("stale", msg)

    def test_empty_frame_is_left_to_caller(self):
        """빈 데이터프레임은 이 보호 장치가 아닌 호출자에게 맡겨지는지 검증하는 테스트."""
        # 빈 프레임은 다른 곳에서 처리하는 데이터 없음 상황이지, 오래됨 상황이 아니다.
        _assert_ohlcv_not_stale(
            pd.DataFrame(columns=["Date", "Close"]), "2026-06-11", "X"
        )

    def test_long_holiday_gap_within_threshold_is_accepted(self):
        """긴 연휴 수준의 공백은 임계값(threshold) 안이면 허용되는지 검증하는 테스트."""
        _assert_ohlcv_not_stale(_frame("2026-06-02"), "2026-06-11", "X")  # 9일 간격


@pytest.mark.unit
class StaleGuardPropagationTests(unittest.TestCase):
    def test_get_yfin_data_online_raises_on_stale_frame(self):
        """get_YFin_data_online이 오래된 프레임에 대해 예외를 발생시키는지 검증하는 테스트."""
        stale = pd.DataFrame(
            {
                "Open": [280.0], "High": [286.0], "Low": [278.0],
                "Close": [284.45], "Volume": [1_000_000],
            },
            index=pd.DatetimeIndex([pd.Timestamp("2025-06-11")], name="Date"),
        )

        class DummyTicker:
            def __init__(self, symbol):
                pass

            def history(self, start, end):
                return stale

        with mock.patch.object(y_finance.yf, "Ticker", DummyTicker), \
                self.assertRaises(NoMarketDataError):
            y_finance.get_YFin_data_online("CB", "2026-06-01", "2026-06-11")


@pytest.mark.unit
class StaleGuardRoutingTests(unittest.TestCase):
    def setUp(self):
        config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)

    def tearDown(self):
        config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)

    def test_router_sentinel_surfaces_stale_reason(self):
        """라우터의 센티널 메시지에 오래됨(stale) 사유가 드러나는지 검증하는 테스트."""
        set_config({"data_vendors": {"core_stock_apis": "yfinance"}})

        def _stale(symbol, *a, **k):
            raise NoMarketDataError(
                symbol, symbol, "latest row is 2025-06-11, 365 days before ... (stale)"
            )

        with mock.patch.dict(
            interface.VENDOR_METHODS,
            {"get_stock_data": {"yfinance": _stale}},
            clear=False,
        ):
            out = interface.route_to_vendor(
                "get_stock_data", "CB", "2026-06-01", "2026-06-11"
            )
        self.assertIn("NO_DATA_AVAILABLE", out)
        self.assertIn("stale", out)  # 타입이 지정된 상세 사유가 에이전트에 전달됨


if __name__ == "__main__":
    unittest.main()
