"""[모듈 개요] 프롬프트-데이터 정합성 테스트 (설계분석 단기 로드맵 #5, #7).

세 가지를 검증한다:

1. 트레이더/리서치 매니저/포트폴리오 매니저의 프롬프트에 분석가 4종
   원본 보고서(market/sentiment/news/fundamentals)가 실제로 포함되는지 —
   "분석가 보고서에 근거하라"는 지시와 제공 자료의 불일치(환각 원인)를 막는다.
   또한 분석가 일부만 선택된 실행(보고서 키 부재)에서도 크래시하지 않는지.
2. Bull/Bear 리서처의 첫 발언 프롬프트에, 반박할 상대 주장이 아직 없을 때
   자기 논거를 제시하라는 폴백 문구가 포함되는지 (리스크 토론자와 동일 패턴).
3. 1단계 분석가 4종의 프롬프트에 "FINAL TRANSACTION PROPOSAL" 최종 제안
   지시가 없는지 — 최상류 분석가가 결론을 내리는 것은 파이프라인 역할과
   모순되며 하류 5단계 등급 체계와 충돌한다.

프롬프트 캡처는 tests/test_structured_agent_prompts.py의 MagicMock 패턴을 따른다.
"""
from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest

from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
from tradingagents.agents.managers.research_manager import create_research_manager
from tradingagents.agents.researchers.bear_researcher import create_bear_researcher
from tradingagents.agents.researchers.bull_researcher import create_bull_researcher
from tradingagents.agents.trader.trader import create_trader

# 분석가 4종 보고서 더미 본문: 프롬프트 포함 여부를 확인하기 위한 고유 마커.
ANALYST_REPORTS = {
    "market_report": "MARKET-REPORT-BODY-XYZ",
    "sentiment_report": "SENTIMENT-REPORT-BODY-XYZ",
    "news_report": "NEWS-REPORT-BODY-XYZ",
    "fundamentals_report": "FUNDAMENTALS-REPORT-BODY-XYZ",
}


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
    """캡처한 프롬프트(문자열, 메시지 목록)를 하나의 텍스트로 평탄화한다."""
    if isinstance(prompt, str):
        return prompt
    parts = []
    for m in prompt:
        parts.append(m.get("content", "") if isinstance(m, dict) else getattr(m, "content", ""))
    return "\n".join(str(p) for p in parts)


# ---------------------------------------------------------------------------
# 트레이더 (Trader)
# ---------------------------------------------------------------------------


def _trader_llm(captured: dict):
    from tradingagents.agents.schemas import TraderAction, TraderProposal

    return _capturing_llm(
        captured, TraderProposal(action=TraderAction.BUY, reasoning="x")
    )


def _trader_state(**extra):
    state = {
        "company_of_interest": "NVDA",
        "investment_plan": "**Recommendation**: Buy",
    }
    state.update(extra)
    return state


@pytest.mark.unit
def test_trader_prompt_includes_all_four_analyst_reports():
    """트레이더 프롬프트에 분석가 4종 보고서 본문이 포함되는지 검증하는 테스트."""
    captured = {}
    create_trader(_trader_llm(captured))(_trader_state(**ANALYST_REPORTS))
    text = _prompt_text(captured["prompt"])
    for body in ANALYST_REPORTS.values():
        assert body in text, f"trader prompt missing report body {body}"


@pytest.mark.unit
def test_trader_does_not_crash_without_reports():
    """보고서 키가 없는 상태(분석가 일부만 실행)에서도 트레이더가 크래시하지 않는지 검증."""
    captured = {}
    result = create_trader(_trader_llm(captured))(_trader_state())
    assert result["trader_investment_plan"]
    # 지시 문구와 제공 자료가 일치해야 한다: 보고서 라벨은 항상 존재한다.
    assert "Market Research Report:" in _prompt_text(captured["prompt"])


# ---------------------------------------------------------------------------
# 리서치 매니저 (Research Manager)
# ---------------------------------------------------------------------------


def _rm_llm(captured: dict):
    from tradingagents.agents.schemas import PortfolioRating, ResearchPlan

    return _capturing_llm(
        captured,
        # bull/bear_case_assessment는 중기 로드맵 #3, 루브릭 점수 6종은
        # 편향검증 Phase 2에서 추가된 필수 필드.
        ResearchPlan(
            bull_evidence_score=0, bear_evidence_score=0,
            bull_responsiveness_score=0, bear_responsiveness_score=0,
            bull_risk_asymmetry_score=0, bear_risk_asymmetry_score=0,
            recommendation=PortfolioRating.BUY,
            bull_case_assessment="ba", bear_case_assessment="be",
            rationale="x", strategic_actions="y",
        ),
    )


def _rm_state(**extra):
    state = {
        "company_of_interest": "NVDA",
        "investment_debate_state": {
            "history": "h", "bull_history": "b", "bear_history": "r",
            "current_response": "", "judge_decision": "", "count": 1,
        },
    }
    state.update(extra)
    return state


@pytest.mark.unit
def test_research_manager_prompt_includes_all_four_analyst_reports():
    """리서치 매니저 프롬프트에 분석가 4종 보고서 본문이 포함되는지 검증하는 테스트."""
    captured = {}
    create_research_manager(_rm_llm(captured))(_rm_state(**ANALYST_REPORTS))
    text = _prompt_text(captured["prompt"])
    for body in ANALYST_REPORTS.values():
        assert body in text, f"research manager prompt missing report body {body}"


@pytest.mark.unit
def test_research_manager_does_not_crash_without_reports():
    """보고서 키가 없는 상태에서도 리서치 매니저가 크래시하지 않는지 검증."""
    captured = {}
    result = create_research_manager(_rm_llm(captured))(_rm_state())
    assert result["investment_plan"]
    assert "Analyst Reports" in _prompt_text(captured["prompt"])


# ---------------------------------------------------------------------------
# 포트폴리오 매니저 (Portfolio Manager)
# ---------------------------------------------------------------------------


def _pm_llm(captured: dict):
    from tradingagents.agents.schemas import PortfolioDecision, PortfolioRating

    return _capturing_llm(
        captured,
        PortfolioDecision(
            rating=PortfolioRating.HOLD,
            executive_summary="x",
            investment_thesis="y",
        ),
    )


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
    }
    state.update(extra)
    return state


@pytest.mark.unit
def test_portfolio_manager_prompt_includes_all_four_analyst_reports():
    """포트폴리오 매니저 프롬프트에 분석가 4종 보고서 본문이 포함되는지 검증하는 테스트."""
    captured = {}
    create_portfolio_manager(_pm_llm(captured))(_pm_state(**ANALYST_REPORTS))
    text = _prompt_text(captured["prompt"])
    for body in ANALYST_REPORTS.values():
        assert body in text, f"portfolio manager prompt missing report body {body}"


@pytest.mark.unit
def test_portfolio_manager_does_not_crash_without_reports():
    """보고서 키가 없는 상태에서도 포트폴리오 매니저가 크래시하지 않는지 검증."""
    captured = {}
    result = create_portfolio_manager(_pm_llm(captured))(_pm_state())
    assert result["final_trade_decision"]
    assert "Analyst Reports" in _prompt_text(captured["prompt"])


# ---------------------------------------------------------------------------
# Bull/Bear 첫 발언 폴백 (설계분석 단기 로드맵 #7)
# ---------------------------------------------------------------------------


def _researcher_state():
    """토론 시작 직후(상대 발언 없음) 상태를 만든다."""
    return {
        "company_of_interest": "NVDA",
        "investment_debate_state": {
            "history": "", "bull_history": "", "bear_history": "",
            "current_response": "", "judge_decision": "", "count": 0,
        },
        **ANALYST_REPORTS,
    }


def _plain_llm(content: str):
    llm = MagicMock()
    llm.invoke.return_value = MagicMock(content=content)
    return llm


@pytest.mark.unit
def test_bull_first_turn_prompt_has_opening_fallback():
    """토론 첫 발언(빈 current_response)에서 Bull 프롬프트에 개시 발언 폴백 문구가 있는지 검증."""
    llm = _plain_llm("bull opening")
    create_bull_researcher(llm)(_researcher_state())
    prompt = llm.invoke.call_args[0][0]
    assert "If there is no bear argument yet" in prompt
    # 폴백 문구는 존재하지 않는 상대 주장 반박 요구(환각 유도)를 대체한다.
    assert "present your own bull case based on the available data" in prompt


@pytest.mark.unit
def test_bear_first_turn_prompt_has_opening_fallback():
    """Bear 프롬프트에도 동일한 개시 발언 폴백 문구(리스크 토론자와 같은 패턴)가 있는지 검증."""
    llm = _plain_llm("bear opening")
    create_bear_researcher(llm)(_researcher_state())
    prompt = llm.invoke.call_args[0][0]
    assert "If there is no bull argument yet" in prompt
    assert "present your own bear case based on the available data" in prompt


# ---------------------------------------------------------------------------
# 1단계 분석가의 최종 제안 지시 제거 (설계분석 단기 로드맵 #7)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_analyst_prompts_do_not_request_final_transaction_proposal():
    """분석가 4종 프롬프트에 FINAL TRANSACTION PROPOSAL 지시가 없는지 검증.

    분석가는 파이프라인 1단계로 보고서만 작성해야 하며, 최종 매수/매도
    제안은 하류(트레이더·포트폴리오 매니저)의 역할이다. 소스 검사 방식은
    test_structured_agent_prompts.py의 date-guidance 테스트 패턴을 따른다.
    """
    import tradingagents.agents.analysts.fundamentals_analyst as fundamentals
    import tradingagents.agents.analysts.market_analyst as market
    import tradingagents.agents.analysts.news_analyst as news
    import tradingagents.agents.analysts.sentiment_analyst as sentiment

    for module in (market, news, fundamentals, sentiment):
        src = inspect.getsource(module)
        assert "FINAL TRANSACTION PROPOSAL" not in src, (
            f"{module.__name__} still instructs a final transaction proposal"
        )
