"""[모듈 개요] 심볼 정규화(normalization)와 데이터 없음 라우팅 센티널(sentinel)을
검증하는 테스트.
"""

import unittest

import pytest

from tradingagents.dataflows.symbol_utils import (
    NoMarketDataError,
    crypto_base,
    is_yahoo_safe,
    normalize_symbol,
)


@pytest.mark.unit
class TestNormalizeSymbol(unittest.TestCase):
    def test_plain_equities_unchanged(self):
        """일반 주식·지수·선물 심볼은 변경 없이 그대로 통과하는지 검증하는 테스트."""
        for sym in ("AAPL", "MSFT", "TSM", "BRK.B", "0700.HK", "^GSPC", "GC=F"):
            self.assertEqual(normalize_symbol(sym), sym)

    def test_lowercases_are_upper(self):
        """소문자와 공백이 있는 입력이 대문자로 정리되는지 검증하는 테스트."""
        self.assertEqual(normalize_symbol("aapl"), "AAPL")
        self.assertEqual(normalize_symbol("  msft  "), "MSFT")

    def test_metal_aliases_map_to_futures(self):
        """금·은 별칭이 해당 선물(futures) 심볼로 매핑되는지 검증하는 테스트."""
        self.assertEqual(normalize_symbol("XAUUSD"), "GC=F")
        self.assertEqual(normalize_symbol("XAUUSD+"), "GC=F")   # 브로커 CFD 접미사
        self.assertEqual(normalize_symbol("xauusd+"), "GC=F")
        self.assertEqual(normalize_symbol("GOLD"), "GC=F")
        self.assertEqual(normalize_symbol("XAGUSD"), "SI=F")

    def test_energy_and_index_aliases(self):
        """에너지·지수 별칭이 올바른 야후 심볼로 매핑되는지 검증하는 테스트."""
        self.assertEqual(normalize_symbol("USOIL"), "CL=F")
        self.assertEqual(normalize_symbol("SPX500"), "^GSPC")
        self.assertEqual(normalize_symbol("NAS100"), "^NDX")
        self.assertEqual(normalize_symbol("US30"), "^DJI")

    def test_forex_pairs_get_x_suffix(self):
        """외환(forex) 페어에 =X 접미사가 붙는지 검증하는 테스트."""
        self.assertEqual(normalize_symbol("EURUSD"), "EURUSD=X")
        self.assertEqual(normalize_symbol("GBPJPY"), "GBPJPY=X")
        self.assertEqual(normalize_symbol("eurusd"), "EURUSD=X")

    def test_crypto_pairs_get_dash_usd(self):
        """암호화폐 페어가 대시(-USD) 형식으로 변환되는지 검증하는 테스트."""
        self.assertEqual(normalize_symbol("BTCUSD"), "BTC-USD")
        self.assertEqual(normalize_symbol("ETHUSD"), "ETH-USD")

    def test_six_letter_non_currency_left_alone(self):
        """통화 코드가 아닌 6글자 티커는 변형되지 않는지 검증하는 테스트."""
        # 두 통화 코드의 조합이 아닌 GOOGLE 스타일의 6글자 티커가
        # 가짜 외환 페어로 잘못 변형되면 안 된다.
        self.assertEqual(normalize_symbol("ABCDEF"), "ABCDEF")

    def test_empty_input_passthrough(self):
        """빈 문자열 입력이 그대로 반환되는지 검증하는 테스트."""
        self.assertEqual(normalize_symbol(""), "")


@pytest.mark.unit
class TestNoMarketDataError(unittest.TestCase):
    def test_message_includes_resolution(self):
        """오류 메시지에 원본 심볼과 해석된 심볼이 모두 포함되는지 검증하는 테스트."""
        err = NoMarketDataError("XAUUSD+", "GC=F", "no rows")
        self.assertIn("XAUUSD+", str(err))
        self.assertIn("GC=F", str(err))
        self.assertEqual(err.symbol, "XAUUSD+")
        self.assertEqual(err.canonical, "GC=F")

    def test_canonical_defaults_to_symbol(self):
        """표준(canonical) 심볼을 생략하면 원본 심볼이 기본값이 되는지 검증하는 테스트."""
        err = NoMarketDataError("FOOBAR")
        self.assertEqual(err.canonical, "FOOBAR")


@pytest.mark.unit
class TestIsYahooSafe(unittest.TestCase):
    def test_accepts_structural_chars(self):
        """야후 심볼의 구조적 문자(=, ^, ., -)가 허용되는지 검증하는 테스트."""
        for sym in ("AAPL", "GC=F", "^GSPC", "BRK.B", "BTC-USD"):
            self.assertTrue(is_yahoo_safe(sym))

    def test_rejects_slash_and_space(self):
        """슬래시나 공백이 있는 심볼이 거부되는지 검증하는 테스트."""
        for sym in ("a/b", "AA PL", ""):
            self.assertFalse(is_yahoo_safe(sym))


@pytest.mark.unit
class TestCryptoBase(unittest.TestCase):
    def test_resolves_known_crypto_forms(self):
        """알려진 암호화폐 표기 형태들에서 기초 심볼(base)이 추출되는지 검증하는 테스트."""
        for raw in ("BTC-USD", "BTCUSD", "btc-usdt", "BTC-USDC", "BTCUSD+"):
            self.assertEqual(crypto_base(raw), "BTC")
        self.assertEqual(crypto_base("ETH-USD"), "ETH")
        self.assertEqual(crypto_base("sol-usd"), "SOL")

    def test_non_crypto_returns_none(self):
        """암호화폐가 아닌 입력에는 None을 반환하는지 검증하는 테스트."""
        # 일반 주식, 클래스 주식, 다른 경로에서 별칭 처리되는 실제 티커
        # (GOLD -> 야후 경로에서는 금 선물)는 암호화폐로 읽히면 안 된다.
        for raw in ("AAPL", "BRK-B", "GOLD", "XYZ-USD", "EURUSD", "", None):
            self.assertIsNone(crypto_base(raw))

    def test_agrees_with_normalize_symbol(self):
        """crypto_base와 normalize_symbol의 결과가 서로 일관되는지 검증하는 테스트."""
        # crypto_base는 -USD 정규화의 기반이 되는 공용 기본 함수(primitive)다.
        self.assertEqual(normalize_symbol("BTCUSD"), "BTC-USD")
        self.assertEqual(crypto_base("BTCUSD"), "BTC")


if __name__ == "__main__":
    unittest.main()
