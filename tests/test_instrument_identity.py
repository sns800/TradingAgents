# 이 파일은 종목의 정체성(회사명·섹터 등)을 결정론적으로 해석하는 기능(#814)과,
# 메시지 삭제 후 남기는 자리표시자에 종목 컨텍스트를 담는 기능(#888)을
# 검증하는 테스트 모음입니다.
"""결정론적 종목 정체성(instrument-identity) 해석(#814)과
컨텍스트가 고정된(context-anchored) 메시지 자리표시자(#888) 테스트."""

import unittest
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage

from tradingagents.agents.utils.agent_utils import (
    build_instrument_context,
    create_msg_delete,
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


@pytest.mark.unit
class ContextAnchoredPlaceholderTests(unittest.TestCase):
    """#888 — 메시지 삭제 후 자리표시자가 맨몸의 'Continue'여서는 안 됨을 검증하는 테스트 묶음."""

    def _run(self, state_extra):
        state = {
            "messages": [
                HumanMessage(content="old", id="h1"),
                AIMessage(content="reply", id="a1"),
            ],
            **state_extra,
        }
        return create_msg_delete()(state)

    def test_placeholder_is_not_bare_continue(self):
        """자리표시자 메시지가 단순 "Continue"가 아닌지 검증하는 테스트."""
        result = self._run(
            {"company_of_interest": "EC", "asset_type": "stock", "trade_date": "2026-05-28"}
        )
        placeholder = result["messages"][-1]
        self.assertIsInstance(placeholder, HumanMessage)
        self.assertNotEqual(placeholder.content.strip(), "Continue")

    def test_placeholder_carries_resolved_identity(self):
        """자리표시자에 해석된 정체성(회사명)과 거래 날짜가 담기는지 검증하는 테스트."""
        result = self._run(
            {
                "company_of_interest": "EC",
                "instrument_context": "The instrument to analyze is `EC`. Resolved identity: Company: Ecopetrol.",
                "trade_date": "2026-05-28",
            }
        )
        content = result["messages"][-1].content
        self.assertIn("Ecopetrol", content)
        self.assertIn("2026-05-28", content)

    def test_old_messages_are_removed(self):
        """기존 메시지는 모두 삭제되고 자리표시자 하나만 남는지 검증하는 테스트."""
        result = self._run({"company_of_interest": "EC", "trade_date": "2026-05-28"})
        removals = [m for m in result["messages"] if isinstance(m, RemoveMessage)]
        humans = [m for m in result["messages"] if isinstance(m, HumanMessage)]
        self.assertEqual(len(removals), 2)
        self.assertEqual(len(humans), 1)

    def test_safe_defaults_when_state_minimal(self):
        """상태 정보가 최소한일 때도 안전한 기본값으로 동작하는지 검증하는 테스트."""
        result = create_msg_delete()({"messages": [], "company_of_interest": "EC"})
        placeholder = result["messages"][-1]
        self.assertNotEqual(placeholder.content.strip(), "Continue")
        self.assertIn("EC", placeholder.content)


if __name__ == "__main__":
    unittest.main()
