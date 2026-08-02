"""[모듈 개요] 스키마 전용 구조화 출력(structured output) 경로의 에이전트가
도구 호출(tool call)을 유도하지 않는지 검증하는 테스트 (#1130).

`with_structured_output`은 정확히 하나의 도구(스키마)만 바인딩한다. 도구 사용을
부추기는 프롬프트는 모델이 알 수 없는 `web_search` 호출을 내뱉게 만들고,
그러면 구조화 시도가 폐기되어 자유 텍스트 재시도가 강제된다 — LLM 왕복이
한 번 더 발생하고 타입 지정 출력도 잃는다.

이 테스트들은 제약 문구가 모듈에서 상수로 참조되는 것에 그치지 않고,
각 에이전트가 실제로 보내는 *렌더링된* 프롬프트에 도달하는지 확인한다.
"""
from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest

import tradingagents.agents.analysts.sentiment_analyst as sentiment
from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
from tradingagents.agents.managers.research_manager import create_research_manager
from tradingagents.agents.trader.trader import create_trader
from tradingagents.agents.utils.structured import NO_EXTERNAL_TOOLS


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


@pytest.mark.unit
def test_trader_prompt_states_constraint():
    """트레이더(trader) 에이전트의 실제 프롬프트에 외부 도구 금지 문구가 포함되는지 검증하는 테스트."""
    from tradingagents.agents.schemas import TraderAction, TraderProposal

    captured = {}
    llm = _capturing_llm(captured, TraderProposal(action=TraderAction.BUY, reasoning="x"))
    create_trader(llm)({
        "company_of_interest": "NVDA",
        "investment_plan": "**Recommendation**: Buy",
    })
    assert NO_EXTERNAL_TOOLS in _prompt_text(captured["prompt"])


@pytest.mark.unit
def test_research_manager_prompt_states_constraint():
    """리서치 매니저의 실제 프롬프트에 외부 도구 금지 문구가 포함되는지 검증하는 테스트."""
    from tradingagents.agents.schemas import PortfolioRating, ResearchPlan

    captured = {}
    llm = _capturing_llm(
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
    create_research_manager(llm)({
        "company_of_interest": "NVDA",
        "investment_debate_state": {
            "history": "h", "bull_history": "b", "bear_history": "r",
            "current_response": "", "judge_decision": "", "count": 1,
        },
    })
    assert NO_EXTERNAL_TOOLS in _prompt_text(captured["prompt"])


@pytest.mark.unit
def test_portfolio_manager_prompt_states_constraint():
    """포트폴리오 매니저의 실제 프롬프트에 외부 도구 금지 문구가 포함되는지 검증하는 테스트."""
    from tradingagents.agents.schemas import PortfolioDecision, PortfolioRating

    captured = {}
    llm = _capturing_llm(
        captured,
        PortfolioDecision(
            rm_proposed_rating=PortfolioRating.HOLD,
            override_action="confirm",
            override_rationale="No new risk evidence.",
            rating=PortfolioRating.HOLD,
            executive_summary="x",
            investment_thesis="y",
        ),
    )
    risk = {
        "history": "h", "aggressive_history": "a", "conservative_history": "c",
        "neutral_history": "n", "current_aggressive_response": "",
        "current_conservative_response": "", "current_neutral_response": "",
        "latest_speaker": "Neutral", "count": 1,
    }
    create_portfolio_manager(llm)({
        "company_of_interest": "NVDA",
        "risk_debate_state": risk,
        "investment_plan": "plan",
        "trader_investment_plan": "trader plan",
    })
    assert NO_EXTERNAL_TOOLS in _prompt_text(captured["prompt"])


@pytest.mark.unit
def test_sentiment_prompt_states_constraint(monkeypatch):
    """감성 분석가(sentiment analyst)의 프롬프트에 외부 도구 금지 문구가 포함되는지 검증하는 테스트."""
    from tradingagents.agents.schemas import SentimentBand, SentimentReport

    # 미리 수집되는 소스들을 스텁(stub) 처리해 네트워크 I/O 없이 프롬프트를 만든다.
    monkeypatch.setattr(sentiment, "fetch_stocktwits_messages", lambda *a, **k: "st")
    monkeypatch.setattr(sentiment, "fetch_reddit_posts", lambda *a, **k: "rd")
    monkeypatch.setattr(sentiment.get_news, "func", lambda *a, **k: "news", raising=False)

    captured = {}
    llm = _capturing_llm(captured, SentimentReport(
        overall_band=SentimentBand.BULLISH, overall_score=7.5,
        confidence="high", narrative="n",
    ))
    sentiment.create_sentiment_analyst(llm)({
        "company_of_interest": "NVDA", "trade_date": "2026-01-15",
        # 병렬화(중기 #6) 이후 감성 분석가는 전용 채널만 읽는다.
        "asset_type": "stock", "social_messages": [],
    })
    text = _prompt_text(captured["prompt"])
    assert NO_EXTERNAL_TOOLS in text
    # 이 에이전트는 도구를 바인딩하지 않으므로 도구 날짜 범위 문구가 다시 나타나면 안 된다.
    assert "tool-call date ranges" not in text


@pytest.mark.unit
def test_tool_using_analysts_keep_their_date_guidance():
    """도구를 실제로 쓰는 분석가들은 날짜 범위 안내 문구를 유지하는지 검증하는 테스트."""
    # 실제로 도구를 호출하는 분석가들은 도구 날짜 범위를 고정하는 문구를 유지한다
    # (#836) — 이 수정은 도구를 쓰지 않는 에이전트에만 적용된다.
    import tradingagents.agents.analysts.market_analyst as market
    import tradingagents.agents.analysts.news_analyst as news
    for module in (market, news):
        assert "tool-call date ranges" in inspect.getsource(module)


@pytest.mark.unit
def test_constraint_text_is_unambiguous():
    """제약 상수 문구가 명확하고 템플릿과 충돌하지 않는지 검증하는 테스트."""
    assert "do not call external tools" in NO_EXTERNAL_TOOLS.lower()
    # 템플릿 중괄호 금지: 이 문구는 ChatPromptTemplate 문자열에 삽입되는데,
    # 중괄호는 입력 변수로 파싱되기 때문이다.
    assert "{" not in NO_EXTERNAL_TOOLS and "}" not in NO_EXTERNAL_TOOLS
