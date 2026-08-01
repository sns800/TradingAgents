# 이 파일은 yfinance 벤더 함수들이 예외를 "Error ..." 문자열로 삼키지 않고
# 그대로 전파하는지 검증하는 테스트 모음입니다 (설계 분석 단기 #4).
# 라우터(interface.route_to_vendor)의 폴백·first_error·NO_DATA 센티널이
# 전부 예외 기반이므로, 벤더가 예외를 문자열로 바꿔 '정상 반환'하면
# 그 안전장치가 통째로 우회되고 원시 오류가 LLM 컨텍스트에 유입됩니다.
"""yfinance 예외 전파 테스트 (설계분석-보고서 2.5절 단기 #4).

검증 항목:
1. 벤더 함수가 문자열 대신 예외를 전파한다.
2. 라우터가 그 예외를 받아 다음 벤더로 폴백하거나 오류를 드러낸다.
"""
from __future__ import annotations

from unittest import mock

import pytest

import tradingagents.dataflows.y_finance as yfin
import tradingagents.dataflows.yfinance_news as ynews
from tradingagents.dataflows import interface
from tradingagents.dataflows.config import set_config


class _BoomTicker:
    """모든 데이터 속성 접근에서 예외를 던지는 가짜 yf.Ticker."""

    def __init__(self, symbol):
        pass

    def _boom(self):
        raise RuntimeError("simulated yfinance failure")

    @property
    def info(self):
        self._boom()

    @property
    def quarterly_balance_sheet(self):
        self._boom()

    @property
    def balance_sheet(self):
        self._boom()

    @property
    def quarterly_cashflow(self):
        self._boom()

    @property
    def cashflow(self):
        self._boom()

    @property
    def quarterly_income_stmt(self):
        self._boom()

    @property
    def income_stmt(self):
        self._boom()

    @property
    def insider_transactions(self):
        self._boom()

    def get_news(self, count=None):
        self._boom()


class _BoomSearch:
    def __init__(self, *a, **k):
        raise RuntimeError("simulated yfinance search failure")


@pytest.mark.unit
class TestVendorFunctionsPropagateExceptions:
    """벤더 함수 7곳이 예외를 문자열로 바꾸지 않고 전파하는지 검증하는 테스트 모음."""

    @pytest.mark.parametrize(
        "call",
        [
            lambda: yfin.get_fundamentals("AAPL", "2026-01-01"),
            lambda: yfin.get_balance_sheet("AAPL", "quarterly", "2026-01-01"),
            lambda: yfin.get_cashflow("AAPL", "quarterly", "2026-01-01"),
            lambda: yfin.get_income_statement("AAPL", "quarterly", "2026-01-01"),
            lambda: yfin.get_insider_transactions("AAPL", "2026-01-01"),
        ],
        ids=["fundamentals", "balance_sheet", "cashflow", "income_statement", "insider"],
    )
    def test_y_finance_functions_raise(self, call, monkeypatch):
        """y_finance 벤더 함수가 예외를 그대로 전파하는지 검증하는 테스트."""
        monkeypatch.setattr(yfin.yf, "Ticker", _BoomTicker)
        with pytest.raises(RuntimeError, match="simulated yfinance failure"):
            call()

    def test_news_raises(self, monkeypatch):
        """종목 뉴스 함수가 'Error ...' 문자열 대신 예외를 전파하는지 검증하는 테스트."""
        monkeypatch.setattr(ynews.yf, "Ticker", _BoomTicker)
        with pytest.raises(RuntimeError, match="simulated yfinance failure"):
            ynews.get_news_yfinance("AAPL", "2026-01-01", "2026-01-10")

    def test_global_news_raises(self, monkeypatch):
        """글로벌 뉴스 함수가 'Error ...' 문자열 대신 예외를 전파하는지 검증하는 테스트."""
        monkeypatch.setattr(ynews.yf, "Search", _BoomSearch)
        with pytest.raises(RuntimeError, match="simulated yfinance search failure"):
            ynews.get_global_news_yfinance("2026-01-01", look_back_days=7, limit=5)


@pytest.mark.unit
class TestRouterHandlesPropagatedExceptions:
    """전파된 예외를 라우터가 폴백/오류 노출로 처리하는지 검증하는 테스트 모음."""

    def test_router_falls_back_to_next_vendor(self):
        """yfinance 실패 시 라우터가 다음 벤더(alpha_vantage)로 폴백하는지 검증하는 테스트."""
        set_config({"data_vendors": {"news_data": "yfinance,alpha_vantage"}})

        def _broken(*a, **k):
            raise RuntimeError("simulated yfinance failure")

        with mock.patch.dict(
            interface.VENDOR_METHODS,
            {"get_insider_transactions": {
                "yfinance": _broken,
                "alpha_vantage": lambda *a, **k: "AV DATA",
            }},
            clear=False,
        ):
            out = interface.route_to_vendor("get_insider_transactions", "AAPL", "2026-01-01")
        assert out == "AV DATA"

    def test_sole_vendor_error_surfaces(self):
        """유일한 벤더의 실패가 삼켜지지 않고 예외로 드러나는지 검증하는 테스트."""
        set_config({"data_vendors": {"core_stock_apis": "yfinance"}})

        def _broken(*a, **k):
            raise RuntimeError("simulated yfinance failure")

        with mock.patch.dict(
            interface.VENDOR_METHODS,
            {"get_stock_data": {"yfinance": _broken}},
            clear=False,
        ), pytest.raises(RuntimeError, match="simulated yfinance failure"):
            interface.route_to_vendor("get_stock_data", "AAPL", "2026-01-01", "2026-01-10")

    def test_error_string_never_reaches_caller(self, monkeypatch):
        """실제 yfinance 벤더 경유 시 'Error ...' 문자열이 반환되지 않는지 검증하는 테스트."""
        # 라우팅 계층을 통째로 태워, 벤더 구현이 진짜로 예외를 던지고
        # (단일 벤더 설정이므로) 그 예외가 밖으로 드러나는지 확인한다.
        set_config({"data_vendors": {"fundamental_data": "yfinance"}})
        monkeypatch.setattr(yfin.yf, "Ticker", _BoomTicker)
        with pytest.raises(RuntimeError):
            interface.route_to_vendor("get_fundamentals", "AAPL", "2026-01-01")
