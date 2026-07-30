# 이 파일은 CLI의 종목 심볼(symbol) 검증·정규화·자산 유형 분류가
# 실제 데이터 조회 경로와 일치하는지 검증하는 테스트 모음입니다.
"""CLI 심볼 검증/분류는 데이터 경로와 일치해야 함.

다음 이슈들의 회귀(regression) 방지용: #980 (검증이 GC=F를 거부),
#981 (BTCUSD가 주식으로 오분류), #982 (BTC-USDT는 통과되지만
Yahoo에서 가격 조회 불가).
"""
import pytest

from cli.models import AssetType
from cli.utils import detect_asset_type, is_valid_ticker_input, normalize_ticker_symbol
from tradingagents.dataflows.symbol_utils import normalize_symbol


# --- #982: 스테이블코인(stablecoin) 표시 암호화폐는 Yahoo의 -USD 쌍으로 정규화 ---
@pytest.mark.parametrize("raw,expected", [
    ("BTCUSD", "BTC-USD"),
    ("BTCUSDT", "BTC-USD"),
    ("BTC-USDT", "BTC-USD"),
    ("BTC-USDC", "BTC-USD"),
    ("ethusdt", "ETH-USD"),
    # 암호화폐가 아닌 심볼은 그대로 유지되어야 함
    ("AAPL", "AAPL"),
    ("GC=F", "GC=F"),
    ("600519.SS", "600519.SS"),
    ("EURUSD", "EURUSD=X"),
])
def test_normalize_symbol_crypto_and_passthrough(raw, expected):
    """암호화폐 심볼은 표준형으로 정규화하고 나머지는 그대로 통과시키는지 검증하는 테스트."""
    assert normalize_symbol(raw) == expected


# --- #980: 검증이 Yahoo 선물(futures)/외환(forex) 심볼을 허용해야 함 ---
@pytest.mark.parametrize("value,ok", [
    ("GC=F", True),
    ("EURUSD=X", True),
    ("AAPL", True),
    ("0700.HK", True),
    ("^GSPC", True),
    ("", True),                 # 빈 값 -> 이후 단계에서 SPY 기본값 사용
    ("bad symbol!", False),     # 공백 + '!' 는 거부
    ("A" * 40, False),          # 너무 긴 입력
])
def test_ticker_input_validation(value, ok):
    """티커(ticker) 입력 검증이 유효/무효 심볼을 올바르게 판별하는지 검증하는 테스트."""
    assert is_valid_ticker_input(value) is ok


# --- #981/#982: 자산 유형은 정규화된(canonical) 심볼 기준으로 분류 ---
@pytest.mark.parametrize("raw,expected", [
    ("BTCUSD", AssetType.CRYPTO),
    ("BTC-USDT", AssetType.CRYPTO),
    ("BTC-USD", AssetType.CRYPTO),
    ("ETHUSD", AssetType.CRYPTO),
    ("AAPL", AssetType.STOCK),
    ("GC=F", AssetType.STOCK),
    ("600519.SS", AssetType.STOCK),
])
def test_detect_asset_type(raw, expected):
    """심볼로부터 자산 유형(암호화폐/주식)을 올바르게 판별하는지 검증하는 테스트."""
    assert detect_asset_type(raw) == expected


def test_cli_normalize_delegates_to_data_layer():
    """CLI의 심볼 정규화가 데이터 계층의 정규화와 항상 같은 결과를 내는지 검증하는 테스트."""
    # CLI는 데이터 경로가 실제로 가격을 조회할 정규 심볼과 동일한 값을 만들어야 합니다.
    for raw in ("XAUUSD", "BTCUSD", "btc-usdt", "AAPL"):
        assert normalize_ticker_symbol(raw) == normalize_symbol(raw)
