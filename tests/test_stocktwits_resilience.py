"""[모듈 개요] 스톡트윗(StockTwits) 수집을 검증하는 테스트: 전송 오류(transport error)
회복력(#1024)과 암호화폐 심볼 매핑(#1113)을 다룬다.

StockTwits는 암호화폐를 ``<BASE>.X`` 형식으로 등록하며(야후식 ``BTC-USD``는 404),
어떤 전송 오류도 예외를 던지는 대신 자리 표시 문구(placeholder)로 대응해야 한다.
"""

from __future__ import annotations

import http.client
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from tradingagents.dataflows import stocktwits


def _raise(exc):
    class _Resp:
        def __enter__(self_inner):
            return self_inner

        def __exit__(self_inner, *a):
            return False

        def read(self_inner):
            raise exc
    return _Resp()


@pytest.mark.unit
class TestStockTwitsResilience:
    @pytest.mark.parametrize(
        "exc",
        [
            http.client.IncompleteRead(b""),
            HTTPError("url", 503, "down", {}, None),
            TimeoutError("slow"),
        ],
    )
    def test_transport_errors_return_placeholder(self, exc):
        """각종 전송 오류 시 예외 대신 자리 표시 문구를 반환하는지 검증하는 테스트."""
        with patch.object(stocktwits, "urlopen", return_value=_raise(exc)):
            out = stocktwits.fetch_stocktwits_messages("NVDA")
        assert "unavailable" in out.lower()
        assert out.startswith("<stocktwits unavailable")


@pytest.mark.unit
class TestStockTwitsCryptoSymbols:
    @pytest.mark.parametrize(
        ("ticker", "expected"),
        [
            ("BTC-USD", "BTC.X"),
            ("eth-usd", "ETH.X"),
            ("SOL-USD", "SOL.X"),
            ("BTCUSD", "BTC.X"),      # 대시 없는 브로커 형식
            ("BTC-USDT", "BTC.X"),    # 스테이블코인 호가 통화
            ("AMD", "AMD"),
            ("BRK-B", "BRK-B"),       # 대시가 있는 클래스 주식: 그대로 유지
            ("GOLD", "GOLD"),         # 실제 주식(별칭은 다른 곳에서 처리): 여기선 그대로
            ("XYZ-USD", "XYZ-USD"),   # 알 수 없는 기초 심볼: 암호화폐로 취급하지 않음
        ],
    )
    def test_symbol_mapping(self, ticker, expected):
        """티커별 StockTwits 심볼 변환이 기대값과 일치하는지 검증하는 테스트."""
        assert stocktwits._stocktwits_symbol(ticker) == expected

    def test_crypto_pair_requests_dot_x_endpoint(self):
        """암호화폐 페어 요청이 실제로 .X 형식 엔드포인트 URL을 사용하는지 검증하는 테스트."""
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            raise TimeoutError("stop after capturing the URL")

        with patch.object(stocktwits, "urlopen", side_effect=fake_urlopen):
            stocktwits.fetch_stocktwits_messages("BTC-USD")
        assert "/symbol/BTC.X.json" in seen["url"]
