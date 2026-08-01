# 이 파일은 종목의 정체성(회사명·섹터 등)을 결정론적으로 해석하는 기능(#814)을
# 검증하는 테스트 모음입니다.
# (예전에 함께 있던 #888 자리표시자 테스트는 분석가 병렬화 — 설계분석 중기
# 로드맵 #6 — 로 Msg Clear 노드와 create_msg_delete 헬퍼가 제거되면서 함께
# 삭제했습니다. 자리표시자가 필요했던 "공유 대화 비우기" 단계 자체가 더 이상
# 존재하지 않습니다.)
"""결정론적 종목 정체성(instrument-identity) 해석(#814) 테스트."""

import unittest
from unittest.mock import patch

import pytest

from tradingagents.agents.utils.agent_utils import (
    build_instrument_context,
    get_instrument_context_from_state,
    resolve_instrument_identity,
)


@pytest.mark.unit
class ResolveInstrumentIdentityTests(unittest.TestCase):
    """yfinance 메타데이터로부터 종목 정체성을 해석하는 로직을 검증하는 테스트 묶음."""

    def setUp(self):
        resolve_instrument_identity.cache_clear()

    def test_resolves_company_metadata_from_yfinance(self):
        """yfinance 정보에서 회사명·섹터·산업·거래소를 추출하는지 검증하는 테스트."""
        with patch("tradingagents.agents.utils.agent_utils.yf.Ticker") as mock:
            mock.return_value.info = {
                "longName": "TOTO LTD.",
                "shortName": "TOTO",
                "sector": "Industrials",
                "industry": "Building Products & Equipment",
                "exchange": "PNK",
                "quoteType": "EQUITY",
            }
            identity = resolve_instrument_identity("totdy")
        mock.assert_called_once_with("TOTDY")
        self.assertEqual(identity["company_name"], "TOTO LTD.")
        self.assertEqual(identity["sector"], "Industrials")
        self.assertEqual(identity["industry"], "Building Products & Equipment")
        self.assertEqual(identity["exchange"], "PNK")

    def test_falls_back_to_short_name(self):
        """longName이 없으면 shortName으로 대체하는지 검증하는 테스트."""
        with patch("tradingagents.agents.utils.agent_utils.yf.Ticker") as mock:
            mock.return_value.info = {"shortName": "TOTO", "sector": "Industrials"}
            identity = resolve_instrument_identity("TOTDY")
        self.assertEqual(identity["company_name"], "TOTO")

    def test_skips_placeholder_values(self):
        """공백이나 "None" 같은 무의미한 자리표시자 값은 걸러내는지 검증하는 테스트."""
        with patch("tradingagents.agents.utils.agent_utils.yf.Ticker") as mock:
            mock.return_value.info = {"longName": "  ", "sector": "None", "industry": "n/a"}
            identity = resolve_instrument_identity("TOTDY")
        self.assertEqual(identity, {})

    def test_fails_open_on_exception(self):
        """조회 중 예외가 나도 크래시 없이 빈 dict를 반환하는지 검증하는 테스트 (fail-open)."""
        with patch(
            "tradingagents.agents.utils.agent_utils.yf.Ticker",
            side_effect=RuntimeError("rate limited"),
        ):
            self.assertEqual(resolve_instrument_identity("TOTDY"), {})

    def test_result_is_cached(self):
        """같은 심볼의 두 번째 조회는 캐시(cache)에서 처리되는지 검증하는 테스트."""
        with patch("tradingagents.agents.utils.agent_utils.yf.Ticker") as mock:
            mock.return_value.info = {"longName": "TOTO LTD."}
            first = resolve_instrument_identity("TOTDY")
            second = resolve_instrument_identity("TOTDY")
        mock.assert_called_once()  # 두 번째 호출은 캐시에서 처리됨
        self.assertEqual(first, second)


@pytest.mark.unit
class BuildInstrumentContextTests(unittest.TestCase):
    """종목 컨텍스트(context) 문자열 생성 로직을 검증하는 테스트 묶음."""

    def test_mentions_exact_symbol_without_identity(self):
        """정체성 정보 없이도 정확한 심볼과 거래소 접미사 안내가 포함되는지 검증하는 테스트."""
        context = build_instrument_context("7203.T")
        self.assertIn("7203.T", context)
        self.assertIn("exchange suffix", context)
        self.assertNotIn("Resolved identity", context)

    def test_injects_resolved_identity(self):
        """해석된 정체성 정보(회사·섹터·거래소)가 컨텍스트에 주입되는지 검증하는 테스트."""
        context = build_instrument_context(
            "TOTDY", "stock",
            {
                "company_name": "TOTO LTD.",
                "sector": "Industrials",
                "industry": "Building Products & Equipment",
                "exchange": "PNK",
            },
        )
        self.assertIn("Company: TOTO LTD.", context)
        self.assertIn("Industrials / Building Products & Equipment", context)
        self.assertIn("Exchange: PNK", context)
        self.assertIn("Do not substitute a different company", context)

    def test_crypto_uses_name_label_and_keeps_hint(self):
        """암호화폐는 "Company" 대신 "Name" 라벨을 쓰고 암호화폐 힌트를 유지하는지 검증하는 테스트."""
        context = build_instrument_context(
            "BTC-USD", "crypto", {"company_name": "Bitcoin USD"}
        )
        self.assertIn("Name: Bitcoin USD", context)
        self.assertIn("crypto asset rather than a company", context)


@pytest.mark.unit
class GetInstrumentContextFromStateTests(unittest.TestCase):
    """그래프 상태(state)에서 종목 컨텍스트를 가져오는 로직을 검증하는 테스트 묶음."""

    def test_prefers_precomputed_context(self):
        """미리 계산된(precomputed) 컨텍스트가 있으면 그것을 우선 사용하는지 검증하는 테스트."""
        state = {"company_of_interest": "TOTDY", "instrument_context": "PRECOMPUTED"}
        self.assertEqual(get_instrument_context_from_state(state), "PRECOMPUTED")

    def test_fallback_is_network_free_ticker_only(self):
        """대체(fallback) 경로가 네트워크 호출 없이 티커만으로 동작하는지 검증하는 테스트."""
        # instrument_context가 없어도 yfinance 호출 없이 — 네트워크에 접근하면 안 됩니다.
        with patch("tradingagents.agents.utils.agent_utils.yf.Ticker") as mock:
            context = get_instrument_context_from_state(
                {"company_of_interest": "NVDA", "asset_type": "stock"}
            )
        mock.assert_not_called()
        self.assertIn("NVDA", context)

    def test_fallback_respects_asset_type(self):
        """대체 경로도 자산 유형(암호화폐)을 반영하는지 검증하는 테스트."""
        context = get_instrument_context_from_state(
            {"company_of_interest": "BTC-USD", "asset_type": "crypto"}
        )
        self.assertIn("crypto asset", context)


if __name__ == "__main__":
    unittest.main()
