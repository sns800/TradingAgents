"""[모듈 개요] 검증 스냅샷 보존(설계분석 중기 로드맵 #5) 테스트.

하류 에이전트가 시장 분석가의 원본 도구 데이터에 접근하지 못하는 문제
(설계분석-보고서 2.2절 — 원래는 Msg Clear의 파기, 병렬화 이후에는 하류가
분석가 전용 채널을 읽지 않는 구조)에 대한 보존 경로를 검증한다:

  1. 시장 분석가가 도구 결과 중 검증 스냅샷(get_verified_market_snapshot
     출력)을 찾아 별도 상태 필드(verified_snapshot)로 보존하는지
     (NO_DATA 센티널이면 빈 문자열)
  2. 애널리스트 합류 배리어(analyst_join_node — 중기 #6에서 Msg Clear를
     대체)가 verified_snapshot을 포함해 어떤 상태도 건드리지 않는지
  3. 하류 에이전트 8종(리서처 토론자 2, 리스크 토론자 3, 트레이더,
     리서치 매니저, PM)의 프롬프트에 스냅샷 섹션이 주입되고, 스냅샷이
     비어 있으면 섹션이 통째로 생략되는지 (past_context 빈 값 가드와
     동일 패턴)
  4. _run_graph가 PM 완료 후 사후 수치 감사를 적용해, 스냅샷에 없는
     가격 인용에 경고 블록을 덧붙인 결정문이 메모리·시그널로 흐르는지

프롬프트 캡처는 tests/test_prompt_data_alignment.py의 MagicMock 패턴을 따른다.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pandas as pd
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from tradingagents.agents.analysts.market_analyst import (
    _extract_verified_snapshot,
    create_market_analyst,
)
from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
from tradingagents.agents.managers.research_manager import create_research_manager
from tradingagents.agents.researchers.bear_researcher import create_bear_researcher
from tradingagents.agents.researchers.bull_researcher import create_bull_researcher
from tradingagents.agents.risk_mgmt.aggressive_debator import create_aggressive_debator
from tradingagents.agents.risk_mgmt.conservative_debator import create_conservative_debator
from tradingagents.agents.risk_mgmt.neutral_debator import create_neutral_debator
from tradingagents.agents.trader.trader import create_trader
from tradingagents.agents.utils.agent_utils import get_verified_snapshot_block
from tradingagents.agents.utils.market_data_validation_tools import (
    VERIFIED_SNAPSHOT_HEADER_PREFIX,
)
from tradingagents.graph.numeric_audit import AUDIT_WARNING_PREFIX
from tradingagents.graph.propagation import Propagator
from tradingagents.graph.signal_processing import SignalProcessor

# 스냅샷 더미 본문: 프롬프트 포함 여부 확인용 고유 마커(SNAPSHOT-MARKER)를 담는다.
SNAPSHOT_BODY = (
    f"{VERIFIED_SNAPSHOT_HEADER_PREFIX} for NVDA\n\n"
    "SNAPSHOT-MARKER-XYZ\n\n"
    "| Field | Value |\n|---|---:|\n"
    "| Close | 123.45 |\n| Open | 121.00 |\n| Volume | 1000000 |\n"
    "| rsi | 55.10 |\n"
)

# 벤더 라우터/스냅샷 도구가 데이터 부재 시 반환하는 것과 동일한 형태의 센티널.
NO_DATA_SENTINEL = (
    "NO_DATA_AVAILABLE: Could not build a verified market snapshot for 'FAKE' "
    "on 2026-01-01 (No OHLCV data available for FAKE.). No usable OHLCV data "
    "is available for this symbol. Do not estimate or fabricate values — "
    "report that data is unavailable."
)


def _snapshot_tool_message(content: str, with_name: bool = True) -> ToolMessage:
    kwargs = {"name": "get_verified_market_snapshot"} if with_name else {}
    return ToolMessage(content=content, tool_call_id="call-snap", **kwargs)


# ---------------------------------------------------------------------------
# 1. 스냅샷 추출 (도구 메시지 -> 상태 필드)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSnapshotExtraction:
    def test_extracts_by_tool_message_name(self):
        """ToolMessage.name으로 스냅샷 메시지를 식별해 원문을 보존하는지 검증하는 테스트."""
        messages = [
            HumanMessage(content="NVDA"),
            _snapshot_tool_message("custom snapshot body without header"),
        ]
        assert _extract_verified_snapshot(messages) == (
            "custom snapshot body without header"
        )

    def test_extracts_by_header_prefix_fallback(self):
        """name 속성이 없어도 헤더 접두사로 스냅샷을 식별하는지 검증하는 테스트."""
        messages = [_snapshot_tool_message(SNAPSHOT_BODY, with_name=False)]
        assert _extract_verified_snapshot(messages) == SNAPSHOT_BODY

    def test_sentinel_yields_empty_string(self):
        """NO_DATA 센티널 스냅샷은 빈 문자열로 보존(감사·주입 생략)되는지 검증하는 테스트."""
        messages = [_snapshot_tool_message(NO_DATA_SENTINEL)]
        assert _extract_verified_snapshot(messages) == ""

    def test_ignores_other_tool_messages(self):
        """다른 도구(get_stock_data 등)의 결과는 스냅샷으로 오인하지 않는지 검증하는 테스트."""
        messages = [
            ToolMessage(
                content="Date,Open,Close\n2026-01-02,1,2",
                tool_call_id="c1",
                name="get_stock_data",
            ),
            AIMessage(content=f"{VERIFIED_SNAPSHOT_HEADER_PREFIX} mentioned in prose"),
        ]
        assert _extract_verified_snapshot(messages) == ""

    def test_latest_snapshot_wins(self):
        """스냅샷 도구가 여러 번 호출되면 마지막(최신) 출력을 보존하는지 검증하는 테스트."""
        messages = [
            _snapshot_tool_message(f"{VERIFIED_SNAPSHOT_HEADER_PREFIX} for NVDA v1"),
            _snapshot_tool_message(f"{VERIFIED_SNAPSHOT_HEADER_PREFIX} for NVDA v2"),
        ]
        assert _extract_verified_snapshot(messages).endswith("v2")

    def test_header_prefix_matches_real_renderer(self, monkeypatch):
        """폴백 식별 기준(헤더 접두사)이 실제 렌더러 출력과 어긋나지 않는지 검증하는 테스트."""
        import tradingagents.dataflows.market_data_validator as validator

        dates = pd.bdate_range("2026-04-01", "2026-05-20")
        closes = [100.0 + i for i in range(len(dates))]
        df = pd.DataFrame({
            "Date": dates,
            "Open": [c - 0.5 for c in closes],
            "High": [c + 1.0 for c in closes],
            "Low": [c - 1.0 for c in closes],
            "Close": closes,
            "Volume": [1_000_000 + i for i in range(len(dates))],
        })
        monkeypatch.setattr(validator, "load_ohlcv", lambda s, d: df)
        snap = validator.build_verified_market_snapshot("NVDA", "2026-05-13")
        assert snap.startswith(VERIFIED_SNAPSHOT_HEADER_PREFIX)


# ---------------------------------------------------------------------------
# 2. 시장 분석가 노드의 보존 + 합류 배리어의 무간섭
# ---------------------------------------------------------------------------


class _ReportOnlyLLM:
    """bind_tools 후 도구 호출 없는 최종 보고서만 반환하는 가짜 LLM."""

    def bind_tools(self, tools):
        return lambda _prompt_value: AIMessage(content="final market report")


def _analyst_state(tool_message: ToolMessage) -> dict:
    # 시장 분석가는 병렬화(중기 로드맵 #6) 이후 전용 채널(market_messages)만
    # 읽으므로, 테스트 상태도 그 채널에 도구 결과를 담는다.
    return {
        "trade_date": "2026-01-01",
        "company_of_interest": "NVDA",
        "asset_type": "stock",
        "instrument_context": "Instrument: NVDA",
        "market_messages": [
            HumanMessage(content="NVDA"),
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "get_verified_market_snapshot", "args": {}, "id": "call-snap"}
                ],
            ),
            tool_message,
        ],
    }


@pytest.mark.unit
class TestMarketAnalystPreservation:
    def test_node_preserves_snapshot_in_state(self):
        """시장 분석가 노드가 도구 결과의 스냅샷을 verified_snapshot으로 반환하는지 검증하는 테스트."""
        node = create_market_analyst(_ReportOnlyLLM())
        result = node(_analyst_state(_snapshot_tool_message(SNAPSHOT_BODY)))
        assert result["verified_snapshot"] == SNAPSHOT_BODY
        # 기존 필드(market_data_ok 게이트 등)는 그대로 함께 반환되어야 한다.
        assert result["market_data_ok"] is True
        assert result["market_report"] == "final market report"

    def test_node_preserves_empty_on_sentinel(self):
        """센티널(데이터 부재) 스냅샷이면 빈 문자열을 반환하고 게이트 플래그도 내리는지 검증."""
        node = create_market_analyst(_ReportOnlyLLM())
        result = node(_analyst_state(_snapshot_tool_message(NO_DATA_SENTINEL)))
        assert result["verified_snapshot"] == ""
        assert result["market_data_ok"] is False

    def test_initial_state_defaults_to_empty(self):
        """Propagator 초기 상태의 verified_snapshot 기본값이 빈 문자열인지 검증하는 테스트."""
        state = Propagator().create_initial_state("NVDA", "2026-01-01")
        assert state["verified_snapshot"] == ""

    def test_survives_analyst_join(self):
        """합류 배리어(analyst_join_node)가 verified_snapshot을 건드리지 않는지 검증.

        중기 #6에서 Msg Clear가 합류 배리어로 대체됐다. LangGraph는 노드가
        반환한 키만 상태에 병합하므로, 배리어가 빈 갱신을 반환하는 한
        verified_snapshot(과 도구 원본 메시지)은 그대로 생존한다.
        """
        from tradingagents.graph.setup import analyst_join_node

        state = {
            "market_messages": [
                HumanMessage(content="NVDA"),
                _snapshot_tool_message(SNAPSHOT_BODY),
            ],
            "company_of_interest": "NVDA",
            "asset_type": "stock",
            "instrument_context": "Instrument: NVDA",
            "trade_date": "2026-01-01",
            "verified_snapshot": SNAPSHOT_BODY,
        }
        update = analyst_join_node(state)
        assert update == {}, (
            "the analyst join barrier must not mutate any state channel"
        )


# ---------------------------------------------------------------------------
# 3. 하류 에이전트 8종 프롬프트 주입 / 빈 값 생략
# ---------------------------------------------------------------------------

ANALYST_REPORTS = {
    "market_report": "MARKET-REPORT-BODY",
    "sentiment_report": "SENTIMENT-REPORT-BODY",
    "news_report": "NEWS-REPORT-BODY",
    "fundamentals_report": "FUNDAMENTALS-REPORT-BODY",
}

SNAPSHOT_SECTION_LABEL = "Verified market snapshot (authoritative numbers"


def _plain_llm(content: str = "argument"):
    llm = MagicMock()
    llm.invoke.return_value = MagicMock(content=content)
    return llm


def _capturing_llm(captured: dict, result):
    structured = MagicMock()
    structured.invoke.side_effect = lambda prompt: (
        captured.__setitem__("prompt", prompt) or result
    )
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    return llm


def _prompt_text(prompt) -> str:
    if isinstance(prompt, str):
        return prompt
    parts = []
    for m in prompt:
        parts.append(m.get("content", "") if isinstance(m, dict) else getattr(m, "content", ""))
    return "\n".join(str(p) for p in parts)


def _researcher_state(**extra):
    state = {
        "company_of_interest": "NVDA",
        "investment_debate_state": {
            "history": "", "bull_history": "", "bear_history": "",
            "current_response": "", "judge_decision": "", "count": 0,
        },
        **ANALYST_REPORTS,
    }
    state.update(extra)
    return state


def _risk_state(**extra):
    state = {
        "company_of_interest": "NVDA",
        "trader_investment_plan": "trader plan",
        "risk_debate_state": {
            "history": "", "aggressive_history": "", "conservative_history": "",
            "neutral_history": "", "latest_speaker": "",
            "current_aggressive_response": "", "current_conservative_response": "",
            "current_neutral_response": "", "judge_decision": "", "count": 0,
        },
        **ANALYST_REPORTS,
    }
    state.update(extra)
    return state


def _rm_state(**extra):
    state = {
        "company_of_interest": "NVDA",
        "investment_debate_state": {
            "history": "h", "bull_history": "b", "bear_history": "r",
            "current_response": "", "judge_decision": "", "count": 1,
        },
        **ANALYST_REPORTS,
    }
    state.update(extra)
    return state


def _trader_state(**extra):
    state = {
        "company_of_interest": "NVDA",
        "investment_plan": "**Recommendation**: Buy",
        **ANALYST_REPORTS,
    }
    state.update(extra)
    return state


def _pm_state(**extra):
    state = {
        "company_of_interest": "NVDA",
        "risk_debate_state": {
            "history": "h", "aggressive_history": "a", "conservative_history": "c",
            "neutral_history": "n", "current_aggressive_response": "",
            "current_conservative_response": "", "current_neutral_response": "",
            "latest_speaker": "Neutral", "count": 1,
        },
        "investment_plan": "plan",
        "trader_investment_plan": "trader plan",
        **ANALYST_REPORTS,
    }
    state.update(extra)
    return state


def _run_plain_agent(factory, state_builder):
    """자유 텍스트 에이전트(토론자 5종)를 실행하고 프롬프트를 반환한다."""
    llm = _plain_llm()
    factory(llm)(state_builder())
    prompt_no_snap = llm.invoke.call_args[0][0]

    llm2 = _plain_llm()
    factory(llm2)(state_builder(verified_snapshot=SNAPSHOT_BODY))
    prompt_with_snap = llm2.invoke.call_args[0][0]
    return prompt_no_snap, prompt_with_snap


def _run_structured_agent(factory, state_builder, result):
    """구조화 출력 에이전트(트레이더/RM/PM)를 실행하고 프롬프트를 반환한다."""
    captured = {}
    factory(_capturing_llm(captured, result))(state_builder())
    prompt_no_snap = _prompt_text(captured["prompt"])

    captured2 = {}
    factory(_capturing_llm(captured2, result))(
        state_builder(verified_snapshot=SNAPSHOT_BODY)
    )
    prompt_with_snap = _prompt_text(captured2["prompt"])
    return prompt_no_snap, prompt_with_snap


def _structured_results():
    from tradingagents.agents.schemas import (
        PortfolioDecision,
        PortfolioRating,
        ResearchPlan,
        TraderAction,
        TraderProposal,
    )

    return {
        "trader": TraderProposal(action=TraderAction.BUY, reasoning="x"),
        "rm": ResearchPlan(
            # 루브릭 점수 6종은 편향검증 Phase 2에서 추가된 필수 필드.
            bull_evidence_score=0, bear_evidence_score=0,
            bull_responsiveness_score=0, bear_responsiveness_score=0,
            bull_risk_asymmetry_score=0, bear_risk_asymmetry_score=0,
            recommendation=PortfolioRating.BUY,
            bull_case_assessment="ba", bear_case_assessment="be",
            rationale="x", strategic_actions="y",
        ),
        "pm": PortfolioDecision(
            rm_proposed_rating=PortfolioRating.HOLD, override_action="confirm",
            override_rationale="No new risk evidence.",
            rating=PortfolioRating.HOLD, executive_summary="x", investment_thesis="y",
        ),
    }


@pytest.mark.unit
class TestDownstreamPromptInjection:
    @pytest.mark.parametrize(
        "factory,state_builder",
        [
            (create_bull_researcher, _researcher_state),
            (create_bear_researcher, _researcher_state),
            (create_aggressive_debator, _risk_state),
            (create_conservative_debator, _risk_state),
            (create_neutral_debator, _risk_state),
        ],
        ids=["bull", "bear", "aggressive", "conservative", "neutral"],
    )
    def test_debater_prompts_inject_snapshot_and_omit_when_empty(
        self, factory, state_builder
    ):
        """토론자 5종의 프롬프트에 스냅샷 섹션이 주입되고, 빈 값이면 생략되는지 검증하는 테스트."""
        prompt_no_snap, prompt_with_snap = _run_plain_agent(factory, state_builder)

        assert SNAPSHOT_SECTION_LABEL in prompt_with_snap
        assert "SNAPSHOT-MARKER-XYZ" in prompt_with_snap
        # 4종 보고서는 기존대로 함께 주입되어야 한다.
        for body in ANALYST_REPORTS.values():
            assert body in prompt_with_snap

        # 빈 스냅샷이면 섹션 전체 생략 (빈 섹션이 수치 날조를 유도하지 않도록).
        assert SNAPSHOT_SECTION_LABEL not in prompt_no_snap

    @pytest.mark.parametrize(
        "key,factory,state_builder",
        [
            ("trader", create_trader, _trader_state),
            ("rm", create_research_manager, _rm_state),
            ("pm", create_portfolio_manager, _pm_state),
        ],
        ids=["trader", "research-manager", "portfolio-manager"],
    )
    def test_structured_agent_prompts_inject_snapshot_and_omit_when_empty(
        self, key, factory, state_builder
    ):
        """트레이더·리서치 매니저·PM 프롬프트의 스냅샷 주입/빈 값 생략을 검증하는 테스트."""
        result = _structured_results()[key]
        prompt_no_snap, prompt_with_snap = _run_structured_agent(
            factory, state_builder, result
        )

        assert SNAPSHOT_SECTION_LABEL in prompt_with_snap
        assert "SNAPSHOT-MARKER-XYZ" in prompt_with_snap
        for body in ANALYST_REPORTS.values():
            assert body in prompt_with_snap

        assert SNAPSHOT_SECTION_LABEL not in prompt_no_snap

    def test_block_helper_guards_non_string_values(self):
        """스냅샷 필드가 문자열이 아니거나 공백뿐이면 빈 블록을 반환하는지 검증하는 테스트."""
        assert get_verified_snapshot_block({}) == ""
        assert get_verified_snapshot_block({"verified_snapshot": "   "}) == ""
        assert get_verified_snapshot_block({"verified_snapshot": None}) == ""


# ---------------------------------------------------------------------------
# 4. _run_graph 통합 (모킹): PM 완료 후 사후 수치 감사 적용
# ---------------------------------------------------------------------------


def _graph_harness(tmp_path, final_state):
    """LLM 생성 없이 _run_graph의 감사·저장 분기만 실행하는 최소 하네스를 만든다."""
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    g = object.__new__(TradingAgentsGraph)
    g.config = {"results_dir": str(tmp_path)}
    g.debug = False
    g.ticker = final_state["company_of_interest"]
    g.memory_log = MagicMock()
    g.memory_log.get_past_context.return_value = ""
    g.propagator = Propagator()
    g.graph = MagicMock()
    g.graph.invoke.return_value = final_state
    g.signal_processor = SignalProcessor(None)
    g.log_states_dict = {}
    g.resolve_instrument_context = lambda ticker, asset_type="stock": ""
    return g


def _final_state(decision: str, verified_snapshot: str) -> dict:
    state = Propagator().create_initial_state("NVDA", "2026-01-01")
    state.update(
        {
            "investment_plan": "plan",
            "trader_investment_plan": "trader plan",
            "final_trade_decision": decision,
            "verified_snapshot": verified_snapshot,
        }
    )
    state["risk_debate_state"]["judge_decision"] = decision
    return state


@pytest.mark.unit
class TestRunGraphAuditIntegration:
    def test_unsupported_price_gets_warning_and_flows_to_memory(self, tmp_path):
        """스냅샷에 없는 가격 인용에 경고 블록이 붙고, 그 결정문이 메모리로 흐르는지 검증."""
        decision = "**Rating**: Buy\n\nEnter near $999.99 with conviction."
        g = _graph_harness(tmp_path, _final_state(decision, SNAPSHOT_BODY))

        final_state, signal = g._run_graph("NVDA", "2026-01-01")

        audited = final_state["final_trade_decision"]
        assert AUDIT_WARNING_PREFIX in audited
        assert "$999.99" in audited.split(AUDIT_WARNING_PREFIX, 1)[1]
        # 경고는 결정 자체를 바꾸지 않는다: 등급 파싱은 여전히 Buy.
        assert signal == "Buy"
        # 경고가 붙은 결정문이 메모리 로그에 그대로 저장된다.
        stored = g.memory_log.store_decision.call_args.kwargs["final_trade_decision"]
        assert AUDIT_WARNING_PREFIX in stored

    def test_supported_prices_leave_decision_untouched(self, tmp_path):
        """스냅샷 수치와 일치하는 가격 인용에는 경고가 붙지 않는지 검증하는 테스트."""
        decision = "**Rating**: Buy\n\nClose was $123.45; support near $121.00."
        g = _graph_harness(tmp_path, _final_state(decision, SNAPSHOT_BODY))

        final_state, signal = g._run_graph("NVDA", "2026-01-01")
        assert final_state["final_trade_decision"] == decision
        assert signal == "Buy"

    def test_empty_snapshot_skips_audit(self, tmp_path):
        """스냅샷이 비어 있으면(NO_DATA 등) 감사를 생략하고 결정문을 그대로 두는지 검증."""
        decision = "**Rating**: Hold\n\nTarget $999.99 if data confirms."
        g = _graph_harness(tmp_path, _final_state(decision, ""))

        final_state, signal = g._run_graph("NVDA", "2026-01-01")
        assert final_state["final_trade_decision"] == decision
        assert signal == "Hold"

    def test_legacy_state_without_snapshot_key(self, tmp_path):
        """구형 체크포인트(verified_snapshot 키 부재)에서도 크래시 없이 동작하는지 검증."""
        decision = "**Rating**: Sell\n\nExit at $999.99."
        state = _final_state(decision, "")
        del state["verified_snapshot"]
        g = _graph_harness(tmp_path, state)

        final_state, signal = g._run_graph("NVDA", "2026-01-01")
        assert final_state["final_trade_decision"] == decision
        assert signal == "Sell"
