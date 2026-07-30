"""[모듈 개요] 심볼 정규화(normalization)가 가격 조회뿐 아니라 모든 yfinance 경로에
적용되는지 검증하는 테스트.

#983(상품 정체성 조회), #984(리플렉션 수익률), 그리고 뉴스 경로에 대한 회귀
(regression) 테스트: XAUUSD 같은 브로커 심볼은 가격 경로가 쓰는 것과 동일한
야후 심볼(GC=F)로 해석되어야, 정체성·실현 수익률·뉴스 조회가 실패하거나
어긋나지 않고 올바른 상품을 조회한다.
"""
import pandas as pd

import tradingagents.agents.utils.agent_utils as au
import tradingagents.dataflows.yfinance_news as ynews
import tradingagents.graph.trading_graph as tg
from tradingagents.graph.trading_graph import TradingAgentsGraph


def test_identity_lookup_normalizes_symbol(monkeypatch):
    """상품 정체성(identity) 조회 시 심볼이 정규화되는지 검증하는 테스트."""
    seen = {}

    class FakeTicker:
        def __init__(self, symbol):
            seen["symbol"] = symbol

        @property
        def info(self):
            return {"longName": "Gold Futures", "quoteType": "FUTURE"}

    monkeypatch.setattr(au.yf, "Ticker", FakeTicker)
    au.resolve_instrument_identity.cache_clear()

    identity = au.resolve_instrument_identity("XAUUSD")

    assert seen["symbol"] == "GC=F"  # 원본 브로커 심볼이 아니라 정규화된 심볼
    assert identity.get("company_name") == "Gold Futures"


def test_fetch_returns_normalizes_symbol(monkeypatch):
    """수익률 조회(_fetch_returns) 시 심볼이 정규화되는지 검증하는 테스트."""
    queried = []

    class FakeTicker:
        def __init__(self, symbol):
            queried.append(symbol)

        def history(self, *args, **kwargs):
            return pd.DataFrame({"Close": [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0]})

    monkeypatch.setattr(tg.yf, "Ticker", FakeTicker)

    # _fetch_returns는 ``self``를 쓰지 않는다; 그래프 생성을 피하려고 언바운드로 호출한다.
    raw, alpha, days = TradingAgentsGraph._fetch_returns(
        None, "XAUUSD", "2025-01-02", holding_days=5, benchmark="SPY"
    )

    assert queried[0] == "GC=F"  # 종목 심볼이 정규화됨 (#984)
    assert queried[1] == "SPY"   # 벤치마크는 표준(canonical) 심볼 그대로 유지
    assert raw is not None and days is not None


def test_news_lookup_normalizes_symbol(monkeypatch):
    """뉴스 조회 시 심볼이 정규화되고 출처가 표기되는지 검증하는 테스트."""
    seen = {}

    class FakeTicker:
        def __init__(self, symbol):
            seen["symbol"] = symbol

        def get_news(self, count):
            return []

    monkeypatch.setattr(ynews.yf, "Ticker", FakeTicker)
    monkeypatch.setattr(ynews, "yf_retry", lambda fn: fn())

    out = ynews.get_news_yfinance("XAUUSD", "2025-01-01", "2025-01-10")

    assert seen["symbol"] == "GC=F"   # 뉴스는 표준 심볼로 조회됨
    assert "XAUUSD" in out            # 사용자의 티커는 보고서에 그대로 남음
    assert "GC=F" in out              # 출처(provenance)가 표기됨
