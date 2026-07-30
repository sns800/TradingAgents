# 이 파일은 암호화폐(crypto) 자산 모드 지원을 검증하는 테스트 모음입니다.
# 심볼로 자산 유형을 감지하고, 암호화폐에는 재무제표(fundamentals) 애널리스트를
# 제외하며, 초기 상태에 자산 유형이 포함되는지 확인합니다.
import unittest

from cli.models import AnalystType, AssetType
from cli.utils import detect_asset_type, filter_analysts_for_asset_type
from tradingagents.graph.propagation import Propagator


class CryptoAssetModeTests(unittest.TestCase):
    def test_detects_crypto_pair_symbols(self):
        """암호화폐 거래쌍(pair) 심볼을 대소문자 구분 없이 CRYPTO로 감지하는지 검증하는 테스트."""
        self.assertEqual(detect_asset_type("BTC-USD"), AssetType.CRYPTO)
        self.assertEqual(detect_asset_type("eth-usd"), AssetType.CRYPTO)

    def test_defaults_non_crypto_symbols_to_stock(self):
        """암호화폐가 아닌 심볼은 기본값인 주식(STOCK)으로 분류되는지 검증하는 테스트."""
        self.assertEqual(detect_asset_type("AAPL"), AssetType.STOCK)
        self.assertEqual(detect_asset_type("SPY"), AssetType.STOCK)

    def test_filters_out_fundamentals_analyst_for_crypto(self):
        """암호화폐 모드에서는 재무제표(fundamentals) 애널리스트가 제외되는지 검증하는 테스트."""
        analysts = [
            AnalystType.MARKET,
            AnalystType.SOCIAL,
            AnalystType.NEWS,
            AnalystType.FUNDAMENTALS,
        ]

        self.assertEqual(
            filter_analysts_for_asset_type(analysts, AssetType.CRYPTO),
            [
                AnalystType.MARKET,
                AnalystType.SOCIAL,
                AnalystType.NEWS,
            ],
        )

    def test_keeps_all_analysts_for_stock(self):
        """주식 모드에서는 모든 애널리스트가 그대로 유지되는지 검증하는 테스트."""
        analysts = [
            AnalystType.MARKET,
            AnalystType.SOCIAL,
            AnalystType.NEWS,
            AnalystType.FUNDAMENTALS,
        ]

        self.assertEqual(
            filter_analysts_for_asset_type(analysts, AssetType.STOCK),
            analysts,
        )

    def test_propagator_includes_asset_type_in_initial_state(self):
        """Propagator가 만든 초기 상태(initial state)에 자산 유형이 포함되는지 검증하는 테스트."""
        state = Propagator().create_initial_state(
            "BTC-USD", "2026-04-18", asset_type=AssetType.CRYPTO.value
        )

        self.assertEqual(state["asset_type"], AssetType.CRYPTO.value)


if __name__ == "__main__":
    unittest.main()
