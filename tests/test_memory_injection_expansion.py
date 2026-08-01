# 이 파일은 설계 분석 중기 로드맵 #2 "학습 루프 주입 지점 확대"를 검증하는 테스트 모음입니다:
# (A) past_context가 리서치 매니저·트레이더 프롬프트에 역할별 지시문과 함께
#     주입되고, 비어 있으면 섹션 전체가 생략되는지 (#572 빈 메모리 환각 방지 유지).
# (B) 메모리 항목의 PLAN: 섹션(당시 investment_plan 요약) 저장·파싱 왕복과
#     구형 항목 하위 호환.
# (C) 반성(reflection) 입력에 당시 계획 요약이 포함되고, 프롬프트에 단기 표본
#     유보 문구가 들어가는지.
"""학습 루프 주입 지점 확대 테스트 — 역할별 past_context 주입, PLAN 왕복, 반성 입력."""

from unittest.mock import MagicMock

import pytest

from tradingagents.agents.managers.research_manager import create_research_manager
from tradingagents.agents.trader.trader import create_trader
from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.graph.reflection import Reflector
from tradingagents.graph.trading_graph import TradingAgentsGraph

_SEP = TradingMemoryLog._SEPARATOR

PAST_CONTEXT = (
    "[2026-01-05 | NVDA | Buy | +5.0% | +2.0% | 5d]\n\n"
    "REFLECTION:\nEntered too aggressively after an earnings gap."
)
PLAN_SHORT = "Recommendation: Buy\nRationale: strong datacenter demand."
# 500자 절단 검증용 장문 계획 (약 1000자)
PLAN_LONG = "Recommendation: Hold\n" + "Detailed plan sentence about the thesis. " * 25
DECISION_BUY = "Rating: Buy\nEnter at $189-192, 6% portfolio cap."


# ---------------------------------------------------------------------------
# 공용 헬퍼 (test_structured_agent_prompts.py의 캡처 패턴 재사용)
# ---------------------------------------------------------------------------

def _capturing_llm(captured: dict, result):
    """구조화 바인딩이 전달받은 프롬프트를 기록하는 모의(mock) LLM을 생성한다."""
    structured = MagicMock()
    structured.invoke.side_effect = lambda prompt: (
        captured.__setitem__("prompt", prompt) or result
    )
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    return llm


def _prompt_text(prompt) -> str:
    """캡처한 프롬프트(문자열, 메시지 목록, 객체)를 하나의 텍스트로 평탄화한다."""
    if isinstance(prompt, str):
        return prompt
    parts = []
    for m in prompt:
        parts.append(m.get("content", "") if isinstance(m, dict) else getattr(m, "content", ""))
    return "\n".join(str(p) for p in parts)


def _run_research_manager(state_overrides: dict) -> str:
    """리서치 매니저 노드를 mock LLM으로 실행하고 렌더링된 프롬프트를 반환한다."""
    from tradingagents.agents.schemas import PortfolioRating, ResearchPlan

    captured = {}
    llm = _capturing_llm(
        captured,
        ResearchPlan(
            recommendation=PortfolioRating.BUY, rationale="x", strategic_actions="y"
        ),
    )
    state = {
        "company_of_interest": "NVDA",
        "investment_debate_state": {
            "history": "h", "bull_history": "b", "bear_history": "r",
            "current_response": "", "judge_decision": "", "count": 1,
        },
        **state_overrides,
    }
    create_research_manager(llm)(state)
    return _prompt_text(captured["prompt"])


def _run_trader(state_overrides: dict) -> str:
    """트레이더 노드를 mock LLM으로 실행하고 렌더링된 프롬프트를 반환한다."""
    from tradingagents.agents.schemas import TraderAction, TraderProposal

    captured = {}
    llm = _capturing_llm(
        captured, TraderProposal(action=TraderAction.BUY, reasoning="x")
    )
    state = {
        "company_of_interest": "NVDA",
        "investment_plan": "**Recommendation**: Buy",
        **state_overrides,
    }
    create_trader(llm)(state)
    return _prompt_text(captured["prompt"])


def make_log(tmp_path, **extra):
    config = {"memory_log_path": str(tmp_path / "trading_memory.md"), **extra}
    return TradingMemoryLog(config)


# ---------------------------------------------------------------------------
# 항목 A: 리서치 매니저·트레이더 프롬프트의 past_context 주입/생략
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestPastContextInjection:
    """past_context가 있으면 역할별 지시문과 함께 주입되고, 없으면 완전히 생략되는지."""

    def test_research_manager_includes_past_context(self):
        """past_context가 있으면 리서치 매니저 프롬프트에 교훈 본문과
        판정용(judging) 지시문이 포함되는지 검증하는 테스트."""
        prompt = _run_research_manager({"past_context": PAST_CONTEXT})
        assert PAST_CONTEXT in prompt
        assert "Lessons from past decisions" in prompt
        # 역할별 필터링: 판정 시 참고하라는 지시 (REFLECTION 중심)
        assert "judging this debate" in prompt

    def test_research_manager_omits_section_when_empty(self):
        """past_context가 빈 문자열이면 섹션·지시문이 통째로 생략되는지
        검증하는 테스트 (#572 빈 메모리 환각 방지)."""
        prompt = _run_research_manager({"past_context": ""})
        assert "Lessons from past decisions" not in prompt
        assert "judging this debate" not in prompt

    def test_research_manager_omits_section_when_key_missing(self):
        """상태에 past_context 키 자체가 없어도 안전하게 생략되는지 검증하는 테스트."""
        prompt = _run_research_manager({})
        assert "Lessons from past decisions" not in prompt

    def test_trader_includes_past_context(self):
        """past_context가 있으면 트레이더 프롬프트에 교훈 본문과
        실행 계획 수립용 지시문이 포함되는지 검증하는 테스트."""
        prompt = _run_trader({"past_context": PAST_CONTEXT})
        assert PAST_CONTEXT in prompt
        assert "Lessons from past decisions" in prompt
        # 역할별 필터링: 실행 계획(진입/청산, 포지션 크기, 리스크 통제) 참고 지시
        assert "constructing the execution plan" in prompt

    def test_trader_omits_section_when_empty(self):
        """past_context가 빈 문자열이면 트레이더 프롬프트에서 섹션·지시문이
        통째로 생략되는지 검증하는 테스트 (#572 빈 메모리 환각 방지)."""
        prompt = _run_trader({"past_context": ""})
        assert "Lessons from past decisions" not in prompt
        assert "constructing the execution plan" not in prompt

    def test_trader_omits_section_when_key_missing(self):
        """상태에 past_context 키 자체가 없어도 안전하게 생략되는지 검증하는 테스트."""
        prompt = _run_trader({})
        assert "Lessons from past decisions" not in prompt

    def test_role_specific_instructions_differ(self):
        """같은 past_context라도 리서치 매니저와 트레이더가 서로 다른 역할별
        사용 지시문을 받는지 검증하는 테스트."""
        rm_prompt = _run_research_manager({"past_context": PAST_CONTEXT})
        trader_prompt = _run_trader({"past_context": PAST_CONTEXT})
        assert "judging this debate" in rm_prompt
        assert "judging this debate" not in trader_prompt
        assert "constructing the execution plan" in trader_prompt
        assert "constructing the execution plan" not in rm_prompt


# ---------------------------------------------------------------------------
# 항목 B: 메모리 항목 PLAN: 섹션 왕복 + 구형 항목 하위 호환
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestPlanSectionRoundtrip:
    """store_decision의 investment_plan이 PLAN: 섹션으로 저장·파싱을 왕복하는지."""

    def test_plan_roundtrip(self, tmp_path):
        """짧은 계획은 그대로 저장되고 파싱 시 plan 키로 복원되는지 검증하는 테스트."""
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-05", DECISION_BUY, investment_plan=PLAN_SHORT)
        entry = log.load_entries()[0]
        assert entry["plan"] == PLAN_SHORT
        assert entry["decision"] == DECISION_BUY.strip()
        assert entry["asset_type"] == "stock"

    def test_long_plan_truncated_deterministically(self, tmp_path):
        """장문 계획은 500자에서 결정론적으로 절단되어 저장되는지 검증하는 테스트."""
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-05", DECISION_BUY, investment_plan=PLAN_LONG)
        plan = log.load_entries()[0]["plan"]
        assert plan.endswith("...")
        assert len(plan) <= TradingMemoryLog._PLAN_SNIPPET_CHARS + 3
        assert plan[:100] == PLAN_LONG.strip()[:100]  # 앞부분 보존
        # 같은 입력이면 항상 같은 절단 결과 (결정론)
        log2 = TradingMemoryLog({"memory_log_path": str(tmp_path / "again" / "m.md")})
        log2.store_decision("NVDA", "2026-01-05", DECISION_BUY, investment_plan=PLAN_LONG)
        assert log2.load_entries()[0]["plan"] == plan

    def test_plan_omitted_when_not_provided(self, tmp_path):
        """investment_plan을 넘기지 않으면 PLAN: 섹션이 기록되지 않고 파싱 시
        빈 문자열이 되는지 검증하는 테스트 (기존 호출자 호환)."""
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-05", DECISION_BUY)
        assert "PLAN:" not in (tmp_path / "trading_memory.md").read_text(encoding="utf-8")
        assert log.load_entries()[0]["plan"] == ""

    def test_legacy_entry_without_plan_parses(self, tmp_path):
        """ASSET/PLAN 섹션이 없는 구형 항목도 plan이 빈 문자열로 안전하게
        파싱되는지 검증하는 테스트 (하위 호환)."""
        entry = (
            "[2026-01-05 | AAPL | Buy | +1.0% | +0.5% | 5d]\n\n"
            "DECISION:\nBuy AAPL.\n\n"
            "REFLECTION:\nLegacy lesson."
            + _SEP
        )
        (tmp_path / "trading_memory.md").write_text(entry, encoding="utf-8")
        log = make_log(tmp_path)
        parsed = log.load_entries()[0]
        assert parsed["plan"] == ""
        assert parsed["decision"] == "Buy AAPL."
        assert parsed["reflection"] == "Legacy lesson."

    def test_plan_survives_outcome_update(self, tmp_path):
        """결과 확정(update_with_outcome) 후에도 PLAN 섹션과 나머지 본문이
        보존되는지 검증하는 테스트."""
        log = make_log(tmp_path)
        log.store_decision(
            "NVDA", "2026-01-05", DECISION_BUY,
            asset_type="stock", investment_plan=PLAN_SHORT,
        )
        log.update_with_outcome("NVDA", "2026-01-05", 0.05, 0.02, 5, "Held up.")
        e = log.load_entries()[0]
        assert e["pending"] is False
        assert e["plan"] == PLAN_SHORT
        assert e["decision"] == DECISION_BUY.strip()
        assert e["reflection"] == "Held up."
        assert e["asset_type"] == "stock"

    def test_plan_not_leaked_into_past_context(self, tmp_path):
        """내부 저장용 PLAN: 섹션이 프롬프트 주입용 past_context에 노출되지
        않는지 검증하는 테스트 (ASSET: 태그와 동일한 규칙)."""
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-05", DECISION_BUY, investment_plan=PLAN_SHORT)
        log.update_with_outcome("NVDA", "2026-01-05", 0.05, 0.02, 5, "Lesson.")
        ctx = log.get_past_context("NVDA")
        assert "PLAN:" not in ctx
        assert "Lesson." in ctx


# ---------------------------------------------------------------------------
# 항목 C: 반성 입력의 계획 요약 + 단기 표본 유보 문구
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestReflectionPlanAndHedge:
    """반성 LLM 입력에 당시 계획 요약과 단기 표본 유보 문구가 포함되는지."""

    def _invoke_reflector(self, **kwargs):
        mock_llm = MagicMock()
        mock_llm.invoke.return_value.content = "ok"
        reflector = Reflector(mock_llm)
        reflector.reflect_on_final_decision(
            final_decision=DECISION_BUY, raw_return=0.05, alpha_return=0.02, **kwargs
        )
        messages = mock_llm.invoke.call_args[0][0]
        system = next(content for role, content in messages if role == "system")
        human = next(content for role, content in messages if role == "human")
        return system, human

    def test_plan_excerpt_included_when_provided(self):
        """investment_plan을 넘기면 반성 입력에 계획 발췌 섹션이 포함되는지 검증하는 테스트."""
        _, human = self._invoke_reflector(investment_plan=PLAN_SHORT)
        assert "Investment plan at decision time" in human
        assert PLAN_SHORT in human
        # 계획이 결정문보다 앞에 와서 "근거 → 결정" 순서를 유지한다.
        assert human.index("Investment plan") < human.index("Final Decision:")

    def test_plan_section_omitted_when_absent(self):
        """investment_plan이 없으면(구형 항목·기존 호출자) 계획 섹션이 생략되는지
        검증하는 테스트."""
        _, human = self._invoke_reflector()
        assert "Investment plan" not in human

    def test_long_plan_truncated_in_reflection_input(self):
        """장문 계획을 넘겨도 반성 입력에서는 500자로 결정론적 절단되는지 검증하는 테스트."""
        _, human = self._invoke_reflector(investment_plan=PLAN_LONG)
        assert PLAN_LONG.strip() not in human      # 전문은 미포함
        assert PLAN_LONG.strip()[:100] in human    # 앞부분은 포함
        assert "..." in human                      # 절단 마커

    def test_prompt_contains_short_sample_hedge(self):
        """반성 시스템 프롬프트에 단기 표본 유보 문구(노이즈 가능성, 사후확증
        서사 금지, 당시 가용 정보 기준 판단)가 포함되는지 검증하는 테스트."""
        system, _ = self._invoke_reflector()
        assert "short holding period" in system
        assert "hindsight" in system
        assert "information available at the time" in system

    def test_resolve_passes_plan_to_reflector(self, tmp_path):
        """_resolve_pending_entries가 저장된 PLAN 요약을 리플렉션 입력으로
        전달하는지 검증하는 테스트."""
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-05", DECISION_BUY, investment_plan=PLAN_SHORT)
        mock_reflector = MagicMock()
        mock_reflector.reflect_on_final_decision.return_value = "Lesson."
        mock_graph = MagicMock(spec=TradingAgentsGraph)
        mock_graph.memory_log = log
        mock_graph.reflector = mock_reflector
        mock_graph.config = {"holding_days": 5}
        mock_graph._fetch_returns = MagicMock(return_value=(0.05, 0.02, 5))
        TradingAgentsGraph._resolve_pending_entries(mock_graph, "NVDA")
        kwargs = mock_reflector.reflect_on_final_decision.call_args.kwargs
        assert kwargs["investment_plan"] == PLAN_SHORT

    def test_resolve_passes_empty_plan_for_legacy_entry(self, tmp_path):
        """PLAN 섹션이 없는 구형 pending 항목은 빈 계획으로 안전하게 반성이
        수행되는지 검증하는 테스트 (하위 호환)."""
        entry = (
            "[2026-01-05 | NVDA | Buy | pending]\n\n"
            f"DECISION:\n{DECISION_BUY}"
            + _SEP
        )
        (tmp_path / "trading_memory.md").write_text(entry, encoding="utf-8")
        log = make_log(tmp_path)
        mock_reflector = MagicMock()
        mock_reflector.reflect_on_final_decision.return_value = "Lesson."
        mock_graph = MagicMock(spec=TradingAgentsGraph)
        mock_graph.memory_log = log
        mock_graph.reflector = mock_reflector
        mock_graph.config = {"holding_days": 5}
        mock_graph._fetch_returns = MagicMock(return_value=(0.05, 0.02, 5))
        TradingAgentsGraph._resolve_pending_entries(mock_graph, "NVDA")
        kwargs = mock_reflector.reflect_on_final_decision.call_args.kwargs
        assert kwargs["investment_plan"] == ""
        assert log.get_pending_entries() == []
