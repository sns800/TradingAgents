"""[모듈 개요] 토론 구조 재설계(설계분석 중기 로드맵 #3) 테스트.

네 가지를 검증한다:

1. 응답 보장 종료 조건 — 리서처 토론은 2N+1 발언(선발언자가 마지막 비판에
   1회 재반박), 리스크 토론은 3N+1 발언으로 끝나는지, 라운드 1/2에서 발언
   순서 전개를 전수 시뮬레이션으로 확인한다. 예전의 2N/3N 종료는 항상
   후발언자(Bear/Neutral) 직후에 끝나 선발언자의 응답 기회가 0회였고
   최후 발언 편향이 고정됐다 (설계분석 2.3).
2. debate_first_speaker 설정 — ConditionalLogic의 드리프트 폴백과
   GraphSetup의 토론 진입 엣지가 설정("bull"/"bear")을 따르는지,
   기본값/환경변수 오버라이드가 동작하는지.
3. 심판 평가 루브릭 — 리서치 매니저·포트폴리오 매니저 프롬프트에 루브릭
   (증거 접지 / 미응답 주장 할인 / 리스크 비대칭)이 도달하고, ResearchPlan의
   양측 논거 평가 필드가 렌더링되는지.
4. 토론자 프롬프트의 이력 압축 — 직전 발언은 전문, 그 이전 발언들은 각
   300자로 결정론적 절단되는지. 단 심판(리서치 매니저)은 전체 이력을
   계속 받는지.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tradingagents.agents.utils.debate_context import (
    DEFAULT_SUMMARY_CHARS,
    TRUNCATION_MARKER,
    condense_debate_history,
)
from tradingagents.graph.conditional_logic import ConditionalLogic

# ---------------------------------------------------------------------------
# 1. 응답 보장 종료 조건: 2N+1 / 3N+1 발언 순서 전수 시뮬레이션
# ---------------------------------------------------------------------------

# 시뮬레이션에서 노드 이름 -> 발언 라벨 매핑 (각 토론자 노드가 실제로
# current_response / latest_speaker에 기록하는 접두사와 동일해야 한다).
_RESEARCHER_LABEL = {
    "Bull Researcher": "Bull Analyst: ...",
    "Bear Researcher": "Bear Analyst: ...",
}
_RISK_LABEL = {
    "Aggressive Analyst": "Aggressive",
    "Conservative Analyst": "Conservative",
    "Neutral Analyst": "Neutral",
}


def _simulate_research_debate(logic: ConditionalLogic) -> list[str]:
    """리서처 토론을 발언 -> 라우터 순으로 전개해 방문 노드 시퀀스를 반환한다."""
    speaker = (
        "Bull Researcher" if logic.debate_first_speaker == "bull" else "Bear Researcher"
    )
    sequence: list[str] = []
    count = 0
    while True:
        assert count < 50, "debate did not terminate (runaway loop)"
        sequence.append(speaker)
        count += 1  # 각 토론자 노드는 발언 후 count를 1 올린다
        state = {
            "investment_debate_state": {
                "count": count,
                "current_response": _RESEARCHER_LABEL[speaker],
            }
        }
        nxt = logic.should_continue_debate(state)
        if nxt == "Research Manager":
            sequence.append(nxt)
            return sequence
        speaker = nxt


def _simulate_risk_debate(logic: ConditionalLogic) -> list[str]:
    """리스크 3자 토론을 발언 -> 라우터 순으로 전개해 방문 노드 시퀀스를 반환한다."""
    speaker = "Aggressive Analyst"  # 진입 엣지는 항상 Trader -> Aggressive (setup.py)
    sequence: list[str] = []
    count = 0
    while True:
        assert count < 50, "risk debate did not terminate (runaway loop)"
        sequence.append(speaker)
        count += 1
        state = {
            "risk_debate_state": {
                "count": count,
                "latest_speaker": _RISK_LABEL[speaker],
            }
        }
        nxt = logic.should_continue_risk_analysis(state)
        if nxt == "Portfolio Manager":
            sequence.append(nxt)
            return sequence
        speaker = nxt


@pytest.mark.unit
class TestResponseGuaranteedTermination:
    def test_research_debate_round1_is_2n_plus_1_bull_last(self):
        """라운드 1: Bull-Bear-Bull(3발언 = 2N+1) 후 심판 — 선발언자가 마지막 재반박."""
        logic = ConditionalLogic(max_debate_rounds=1)
        assert _simulate_research_debate(logic) == [
            "Bull Researcher", "Bear Researcher", "Bull Researcher",
            "Research Manager",
        ]

    def test_research_debate_round2_is_2n_plus_1_bull_last(self):
        """라운드 2: 교대 5발언(2N+1) 후 심판, 마지막 발언자는 선발언자(Bull)."""
        logic = ConditionalLogic(max_debate_rounds=2)
        assert _simulate_research_debate(logic) == [
            "Bull Researcher", "Bear Researcher",
            "Bull Researcher", "Bear Researcher",
            "Bull Researcher",
            "Research Manager",
        ]

    def test_research_debate_bear_first_round1(self):
        """선발언자를 bear로 바꾸면 Bear-Bull-Bear 후 심판 — Bear가 마지막 재반박."""
        logic = ConditionalLogic(max_debate_rounds=1, debate_first_speaker="bear")
        assert _simulate_research_debate(logic) == [
            "Bear Researcher", "Bull Researcher", "Bear Researcher",
            "Research Manager",
        ]

    def test_research_debate_bear_first_round2(self):
        """bear 선발언 라운드 2: 교대 5발언 후 심판, 마지막 발언자는 Bear."""
        logic = ConditionalLogic(max_debate_rounds=2, debate_first_speaker="bear")
        assert _simulate_research_debate(logic) == [
            "Bear Researcher", "Bull Researcher",
            "Bear Researcher", "Bull Researcher",
            "Bear Researcher",
            "Research Manager",
        ]

    def test_risk_debate_round1_is_3n_plus_1_aggressive_last(self):
        """라운드 1: A-C-N-A(4발언 = 3N+1) 후 PM — Aggressive가 비판에 응답."""
        logic = ConditionalLogic(max_risk_discuss_rounds=1)
        assert _simulate_risk_debate(logic) == [
            "Aggressive Analyst", "Conservative Analyst", "Neutral Analyst",
            "Aggressive Analyst",
            "Portfolio Manager",
        ]

    def test_risk_debate_round2_is_3n_plus_1_aggressive_last(self):
        """라운드 2: 순환 7발언(3N+1) 후 PM, 마지막 발언자는 Aggressive."""
        logic = ConditionalLogic(max_risk_discuss_rounds=2)
        assert _simulate_risk_debate(logic) == [
            "Aggressive Analyst", "Conservative Analyst", "Neutral Analyst",
            "Aggressive Analyst", "Conservative Analyst", "Neutral Analyst",
            "Aggressive Analyst",
            "Portfolio Manager",
        ]


# ---------------------------------------------------------------------------
# 2. debate_first_speaker 설정: 라우팅 폴백 / 그래프 진입 엣지 / 설정·환경변수
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFirstSpeakerConfig:
    def test_invalid_first_speaker_raises(self):
        """잘못된 선발언자 값은 조용히 무시되지 않고 시작 시점에 실패해야 한다."""
        with pytest.raises(ValueError, match="debate_first_speaker"):
            ConditionalLogic(debate_first_speaker="neutral")

    def test_first_speaker_is_normalized(self):
        """대소문자/공백 표기가 정규화되는지 검증하는 테스트."""
        assert ConditionalLogic(debate_first_speaker=" Bear ").debate_first_speaker == "bear"

    @pytest.mark.parametrize("first,expected", [
        ("bull", "Bull Researcher"),
        ("bear", "Bear Researcher"),
    ])
    def test_drift_label_falls_back_to_first_speaker(self, first, expected):
        """알 수 없는 발언자 라벨(드리프트)이면 설정된 선발언자에게 차례가 간다 (#1088)."""
        logic = ConditionalLogic(max_debate_rounds=1, debate_first_speaker=first)
        state = {"investment_debate_state": {"count": 0, "current_response": "Optimista"}}
        assert logic.should_continue_debate(state) == expected

    def test_default_behavior_unchanged(self):
        """기본값(bull)에서는 기존 라우팅 동작(빈 라벨 -> Bull)이 그대로 보존된다."""
        logic = ConditionalLogic(max_debate_rounds=1)
        state = {"investment_debate_state": {"count": 0, "current_response": ""}}
        assert logic.should_continue_debate(state) == "Bull Researcher"

    @pytest.mark.parametrize("first,entry_node", [
        ("bull", "Bull Researcher"),
        ("bear", "Bear Researcher"),
    ])
    def test_graph_entry_edge_follows_first_speaker(self, first, entry_node):
        """애널리스트 합류 노드가 설정된 선발언자로 연결되는지 검증.

        분석가 병렬화(중기 로드맵 #6)로 토론 진입점이 "마지막 애널리스트의
        Msg Clear 노드"에서 병렬 분기의 합류 배리어(Analyst Join)로 바뀌었다.
        선발언자 설정이 진입 엣지를 결정한다는 계약은 동일하다.
        """
        from tradingagents.graph.analyst_execution import ANALYST_JOIN_NODE
        from tradingagents.graph.setup import GraphSetup

        logic = ConditionalLogic(debate_first_speaker=first)
        setup = GraphSetup(
            quick_thinking_llm=MagicMock(),
            deep_thinking_llm=MagicMock(),
            tool_nodes={"market": MagicMock()},
            conditional_logic=logic,
        )
        workflow = setup.setup_graph(selected_analysts=("market",))
        # StateGraph.edges는 (시작, 끝) 튜플 집합 — 진입 엣지 존재를 확인한다.
        assert (ANALYST_JOIN_NODE, entry_node) in workflow.edges

    def test_default_config_defaults_to_bull(self, monkeypatch):
        """DEFAULT_CONFIG 기본값이 기존 동작인 "bull"인지 검증하는 테스트."""
        import importlib

        import tradingagents.default_config as default_config_module

        monkeypatch.delenv("TRADINGAGENTS_DEBATE_FIRST_SPEAKER", raising=False)
        dc = importlib.reload(default_config_module)
        assert dc.DEFAULT_CONFIG["debate_first_speaker"] == "bull"

    def test_env_override_sets_first_speaker(self, monkeypatch):
        """TRADINGAGENTS_DEBATE_FIRST_SPEAKER 환경변수가 설정을 덮어쓰는지 검증."""
        import importlib

        import tradingagents.default_config as default_config_module

        monkeypatch.setenv("TRADINGAGENTS_DEBATE_FIRST_SPEAKER", "bear")
        dc = importlib.reload(default_config_module)
        assert dc.DEFAULT_CONFIG["debate_first_speaker"] == "bear"
        # 같은 프로세스의 이후 테스트를 위해 모듈 상태를 복원
        monkeypatch.delenv("TRADINGAGENTS_DEBATE_FIRST_SPEAKER", raising=False)
        importlib.reload(default_config_module)


# ---------------------------------------------------------------------------
# 3. 심판 평가 루브릭 + ResearchPlan 평가 필드
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


def _make_research_plan():
    from tradingagents.agents.schemas import PortfolioRating, ResearchPlan

    return ResearchPlan(
        # 루브릭 점수 6종은 편향검증 Phase 2에서 추가된 필수 필드.
        bull_evidence_score=3,
        bear_evidence_score=1,
        bull_responsiveness_score=2,
        bear_responsiveness_score=-2,
        bull_risk_asymmetry_score=1,
        bear_risk_asymmetry_score=0,
        recommendation=PortfolioRating.BUY,
        bull_case_assessment="Bull's demand claim is traceable to the market report.",
        bear_case_assessment="Bear's margin concern was never rebutted by the bull.",
        rationale="x",
        strategic_actions="y",
    )


# 루브릭 3요소가 프롬프트에 실제로 도달하는지 확인하는 마커들.
_RUBRIC_MARKERS = (
    "Evaluation Rubric",
    "Evidence grounding",
    "Responsiveness",
    "Risk asymmetry",
    "left unanswered",  # 미응답 주장 할인
)


@pytest.mark.unit
class TestJudgeRubric:
    def test_research_manager_prompt_contains_rubric(self):
        """리서치 매니저의 실제 프롬프트에 평가 루브릭이 포함되는지 검증."""
        from tradingagents.agents.managers.research_manager import (
            create_research_manager,
        )

        captured = {}
        llm = _capturing_llm(captured, _make_research_plan())
        create_research_manager(llm)({
            "company_of_interest": "NVDA",
            "investment_debate_state": {
                "history": "h", "bull_history": "b", "bear_history": "r",
                "current_response": "", "judge_decision": "", "count": 3,
            },
        })
        for marker in _RUBRIC_MARKERS:
            assert marker in captured["prompt"], f"RM prompt missing rubric marker {marker!r}"

    def test_portfolio_manager_prompt_contains_rubric(self):
        """포트폴리오 매니저의 판정 프롬프트에 평가 루브릭이 포함되는지 검증."""
        from tradingagents.agents.managers.portfolio_manager import (
            create_portfolio_manager,
        )
        from tradingagents.agents.schemas import PortfolioDecision, PortfolioRating

        captured = {}
        llm = _capturing_llm(captured, PortfolioDecision(
            rm_proposed_rating=PortfolioRating.HOLD,
            override_action="confirm",
            override_rationale="No new risk evidence.",
            rating=PortfolioRating.HOLD,
            executive_summary="s",
            investment_thesis="t",
        ))
        create_portfolio_manager(llm)({
            "company_of_interest": "NVDA",
            "risk_debate_state": {
                "history": "h", "aggressive_history": "a",
                "conservative_history": "c", "neutral_history": "n",
                "current_aggressive_response": "", "current_conservative_response": "",
                "current_neutral_response": "", "latest_speaker": "Aggressive",
                "count": 4,
            },
            "investment_plan": "plan",
            "trader_investment_plan": "trader plan",
        })
        for marker in _RUBRIC_MARKERS:
            assert marker in captured["prompt"], f"PM prompt missing rubric marker {marker!r}"

    def test_research_plan_assessments_are_required_and_rendered(self):
        """양측 논거 평가 필드가 필수이며 렌더링에 포함되는지 검증."""
        from pydantic import ValidationError

        from tradingagents.agents.schemas import (
            PortfolioRating,
            ResearchPlan,
            render_research_plan,
        )

        md = render_research_plan(_make_research_plan())
        assert "**Bull Case Assessment**: Bull's demand claim" in md
        assert "**Bear Case Assessment**: Bear's margin concern" in md
        # 기존 렌더 형식(섹션 헤더)은 그대로 보존된다.
        assert "**Recommendation**: Buy" in md
        assert "**Rationale**: x" in md
        assert "**Strategic Actions**: y" in md
        # 평가 필드는 선택이 아니라 필수 — LLM이 채우도록 스키마가 강제한다.
        with pytest.raises(ValidationError):
            ResearchPlan(
                recommendation=PortfolioRating.BUY, rationale="x", strategic_actions="y"
            )


# ---------------------------------------------------------------------------
# 4. 토론자 프롬프트의 이력 압축 (직전 발언 전문 + 과거 300자 절단)
# ---------------------------------------------------------------------------

# 압축 여부를 구분할 수 있도록 300자를 확실히 넘는 발언 본문을 만든다.
_OLD_BULL = "Bull Analyst: " + ("bull-evidence " * 60).strip()   # ~840자
_OLD_BEAR = "Bear Analyst: " + ("bear-critique " * 60).strip()   # ~840자
_LAST_BULL = "Bull Analyst: " + ("latest-bull-point " * 40).strip()  # ~720자
_HISTORY = "\n" + "\n".join([_OLD_BULL, _OLD_BEAR, _LAST_BULL])


@pytest.mark.unit
class TestCondenseDebateHistory:
    def test_latest_statement_kept_in_full(self):
        """마지막(직전) 발언은 전문이 유지되는지 검증하는 테스트."""
        out = condense_debate_history(_HISTORY)
        assert _LAST_BULL in out

    def test_earlier_statements_truncated_to_300_chars(self):
        """이전 발언들은 각각 앞 300자 + 절단 표식으로 줄어드는지 검증."""
        out = condense_debate_history(_HISTORY)
        for old in (_OLD_BULL, _OLD_BEAR):
            assert old not in out  # 전문은 사라지고
            expected = old[:DEFAULT_SUMMARY_CHARS] + TRUNCATION_MARKER
            assert expected in out  # 결정론적 절단본이 남는다

    def test_truncation_is_deterministic(self):
        """같은 입력에는 항상 같은 출력이 나오는지(LLM 요약이 아님) 검증."""
        assert condense_debate_history(_HISTORY) == condense_debate_history(_HISTORY)

    def test_short_statements_not_marked(self):
        """300자 이하 발언은 절단 표식 없이 그대로 유지되는지 검증."""
        short = "\nBull Analyst: short claim\nBear Analyst: short rebuttal"
        out = condense_debate_history(short)
        assert TRUNCATION_MARKER not in out
        assert "Bull Analyst: short claim" in out

    def test_empty_history_passthrough(self):
        """빈 이력(첫 발언 시점)은 그대로 통과하는지 검증하는 테스트."""
        assert condense_debate_history("") == ""

    def test_unknown_format_passthrough(self):
        """화자 라벨이 없는 텍스트(형식 드리프트)는 손대지 않고 반환하는지 검증."""
        blob = "no speaker labels here " * 30
        assert condense_debate_history(blob) == blob

    def test_multiline_statement_stays_one_unit(self):
        """여러 줄(문단) 발언이 한 발언으로 묶여 절단되는지 검증하는 테스트."""
        multi = "\nBull Analyst: first line\n" + ("more detail " * 40) + "\nBear Analyst: last"
        out = condense_debate_history(multi)
        assert TRUNCATION_MARKER in out          # 여러 줄 Bull 발언이 절단되고
        assert "Bear Analyst: last" in out       # 마지막 발언은 전문 유지


def _make_debate_state(history: str) -> dict:
    return {
        "company_of_interest": "NVDA",
        "market_report": "m", "sentiment_report": "s",
        "news_report": "n", "fundamentals_report": "f",
        "investment_debate_state": {
            "history": history,
            "bull_history": "", "bear_history": "",
            "current_response": _LAST_BULL,
            "judge_decision": "", "count": 3,
        },
    }


@pytest.mark.unit
class TestDebaterPromptsUseCondensedHistory:
    def _plain_llm(self, captured: dict):
        llm = MagicMock()
        llm.invoke.side_effect = lambda prompt: (
            captured.__setitem__("prompt", prompt) or MagicMock(content="x")
        )
        return llm

    def test_bear_researcher_gets_condensed_history(self):
        """토론자(Bear) 프롬프트에서 과거 발언은 절단되고 직전 발언은 전문인지 검증."""
        from tradingagents.agents.researchers.bear_researcher import (
            create_bear_researcher,
        )

        captured = {}
        result = create_bear_researcher(self._plain_llm(captured))(
            _make_debate_state(_HISTORY)
        )
        prompt = captured["prompt"]
        assert _LAST_BULL in prompt              # 직전 발언 전문
        assert _OLD_BEAR not in prompt           # 과거 발언 전문은 없음
        assert _OLD_BEAR[:DEFAULT_SUMMARY_CHARS] in prompt  # 300자 절단본은 있음
        # 상태에 저장되는 history 원본은 절단 없이 이어 붙는다.
        assert _OLD_BEAR in result["investment_debate_state"]["history"]

    def test_risk_debator_gets_condensed_history(self):
        """리스크 토론자(Conservative) 프롬프트도 동일한 압축을 적용하는지 검증."""
        from tradingagents.agents.risk_mgmt.conservative_debator import (
            create_conservative_debator,
        )

        old_a = "Aggressive Analyst: " + ("upside-case " * 60).strip()
        last_n = "Neutral Analyst: " + ("balanced-view " * 40).strip()
        history = "\n" + "\n".join([old_a, last_n])
        captured = {}
        create_conservative_debator(self._plain_llm(captured))({
            "company_of_interest": "NVDA",
            "market_report": "m", "sentiment_report": "s",
            "news_report": "n", "fundamentals_report": "f",
            "trader_investment_plan": "plan",
            "risk_debate_state": {
                "history": history,
                "aggressive_history": "", "conservative_history": "",
                "neutral_history": "", "latest_speaker": "Neutral",
                "current_aggressive_response": old_a,
                "current_neutral_response": last_n,
                "count": 2,
            },
        })
        prompt = captured["prompt"]
        assert last_n in prompt                                   # 직전 발언 전문
        assert old_a[:DEFAULT_SUMMARY_CHARS] + TRUNCATION_MARKER in prompt  # 과거 절단본

    def test_research_manager_still_gets_full_history(self):
        """심판(리서치 매니저)은 판정 근거인 전체 이력을 계속 받는지 검증."""
        from tradingagents.agents.managers.research_manager import (
            create_research_manager,
        )

        captured = {}
        llm = _capturing_llm(captured, _make_research_plan())
        create_research_manager(llm)(_make_debate_state(_HISTORY))
        # 과거 발언 전문이 절단 없이 프롬프트에 존재해야 한다.
        assert _OLD_BULL in captured["prompt"]
        assert _OLD_BEAR in captured["prompt"]
        assert TRUNCATION_MARKER not in captured["prompt"]
