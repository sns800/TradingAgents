"""[모듈 개요] 구조화 출력(structured output) 에이전트들을 검증하는 테스트
(트레이더(Trader), 리서치 매니저(Research Manager), 감성 분석가(Sentiment Analyst)).

포트폴리오 매니저(Portfolio Manager)는 tests/test_memory_log.py에서 별도로
다룬다(메모리 로그 → PM 주입 사이클 전체를 실행). 이 파일은 트레이더,
리서치 매니저, 감성 분석가가 동일한 결정적(deterministic) 출력 형태를
공유하도록 추가한 병렬 스키마, 렌더링 함수, 우아한 폴백(graceful fallback)
동작을 다룬다.
"""

from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from tradingagents.agents.analysts.sentiment_analyst import create_sentiment_analyst
from tradingagents.agents.managers.research_manager import create_research_manager
from tradingagents.agents.schemas import (
    PortfolioDecision,
    PortfolioRating,
    ResearchPlan,
    SentimentBand,
    SentimentReport,
    TraderAction,
    TraderProposal,
    render_research_plan,
    render_sentiment_report,
    render_trader_proposal,
)
from tradingagents.agents.trader.trader import create_trader

# ---------------------------------------------------------------------------
# 렌더링(render) 함수
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRenderTraderProposal:
    def test_minimal_required_fields(self):
        """필수 필드만으로 트레이더 제안이 올바르게 렌더링되는지 검증하는 테스트."""
        p = TraderProposal(action=TraderAction.HOLD, reasoning="Balanced setup; no edge.")
        md = render_trader_proposal(p)
        assert "**Action**: Hold" in md
        assert "**Reasoning**: Balanced setup; no edge." in md
        # 마지막의 FINAL TRANSACTION PROPOSAL 줄은 분석가 정지 신호(stop-signal)
        # 문구와 이를 grep하는 외부 코드를 위해 유지된다.
        assert "FINAL TRANSACTION PROPOSAL: **HOLD**" in md

    def test_optional_fields_included_when_present(self):
        """선택 필드(진입가, 손절가 등)가 있으면 렌더링에 포함되는지 검증하는 테스트."""
        p = TraderProposal(
            action=TraderAction.BUY,
            reasoning="Strong technicals + fundamentals.",
            entry_price=189.5,
            stop_loss=178.0,
            position_sizing="6% of portfolio",
        )
        md = render_trader_proposal(p)
        assert "**Action**: Buy" in md
        assert "**Entry Price**: 189.5" in md
        assert "**Stop Loss**: 178.0" in md
        assert "**Position Sizing**: 6% of portfolio" in md
        assert "FINAL TRANSACTION PROPOSAL: **BUY**" in md

    def test_optional_fields_omitted_when_absent(self):
        """선택 필드가 없으면 렌더링에서 생략되는지 검증하는 테스트."""
        p = TraderProposal(action=TraderAction.SELL, reasoning="Guidance cut.")
        md = render_trader_proposal(p)
        assert "Entry Price" not in md
        assert "Stop Loss" not in md
        assert "Position Sizing" not in md
        assert "FINAL TRANSACTION PROPOSAL: **SELL**" in md


@pytest.mark.unit
class TestNullishFloatCoercion:
    """널 유사(nullish) 문자열의 실수(float) 필드 강제 변환을 검증하는 테스트 모음.

    성능이 낮은 LLM은 선택적 실수 필드에 "None"/"N/A"를 써 넣을 수 있다 (#1058);
    이를 None으로 강제 변환해 구조화 호출이 오류 대신 검증을 통과하게 한다.
    """

    def test_trader_nullish_strings_coerce_to_none(self):
        """"None", "N/A" 같은 문자열이 None으로 변환되는지 검증하는 테스트."""
        for sentinel in ("None", "N/A", "null", "-", "", "TBD"):
            p = TraderProposal(
                action=TraderAction.HOLD,
                reasoning="x",
                entry_price=sentinel,
                stop_loss=sentinel,
            )
            assert p.entry_price is None
            assert p.stop_loss is None

    def test_trader_real_numeric_string_still_parses(self):
        """실제 숫자 문자열은 여전히 실수로 파싱되는지 검증하는 테스트."""
        p = TraderProposal(action=TraderAction.BUY, reasoning="x", entry_price="189.5")
        assert p.entry_price == 189.5

    def test_pm_nullish_price_target_coerces_to_none(self):
        """포트폴리오 결정의 목표 주가(price_target)도 널 유사 문자열이 None으로 변환되는지 검증하는 테스트."""
        d = PortfolioDecision(
            rating=PortfolioRating.OVERWEIGHT,
            executive_summary="s",
            investment_thesis="t",
            price_target="N/A",
        )
        assert d.price_target is None


@pytest.mark.unit
class TestRenderResearchPlan:
    def test_required_fields(self):
        """리서치 플랜의 필수 필드가 마크다운으로 렌더링되는지 검증하는 테스트.

        중기 로드맵 #3(심판 루브릭)으로 양측 논거 평가 필드가 필수로
        추가되어, 생성 시 bull/bear_case_assessment도 함께 채운다.
        """
        p = ResearchPlan(
            recommendation=PortfolioRating.OVERWEIGHT,
            bull_case_assessment="Bull's growth claim is grounded in the fundamentals report.",
            bear_case_assessment="Bear never answered the margin-expansion point.",
            rationale="Bull case carried; tailwinds intact.",
            strategic_actions="Build position over two weeks; cap at 5%.",
        )
        md = render_research_plan(p)
        assert "**Recommendation**: Overweight" in md
        assert "**Bull Case Assessment**: Bull's growth claim" in md
        assert "**Bear Case Assessment**: Bear never answered" in md
        assert "**Rationale**: Bull case carried" in md
        assert "**Strategic Actions**: Build position" in md

    def test_all_5_tier_ratings_render(self):
        """5단계 등급 전부가 렌더링되는지 검증하는 테스트."""
        for rating in PortfolioRating:
            p = ResearchPlan(
                recommendation=rating,
                bull_case_assessment="bull-assess",
                bear_case_assessment="bear-assess",
                rationale="r",
                strategic_actions="s",
            )
            md = render_research_plan(p)
            assert f"**Recommendation**: {rating.value}" in md


# ---------------------------------------------------------------------------
# 트레이더 에이전트: 구조화 정상 경로(happy path) + 폴백(fallback)
# ---------------------------------------------------------------------------


def _make_trader_state():
    return {
        "company_of_interest": "NVDA",
        "investment_plan": "**Recommendation**: Buy\n**Rationale**: ...\n**Strategic Actions**: ...",
    }


def _structured_trader_llm(captured: dict, proposal: TraderProposal | None = None):
    """with_structured_output 바인딩이 프롬프트를 캡처하고 실제 TraderProposal을
    반환하는 MagicMock LLM을 생성한다 (render_trader_proposal이 동작하도록).
    """
    if proposal is None:
        proposal = TraderProposal(
            action=TraderAction.BUY,
            reasoning="Strong setup.",
        )
    structured = MagicMock()
    structured.invoke.side_effect = lambda prompt: (
        captured.__setitem__("prompt", prompt) or proposal
    )
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    return llm


@pytest.mark.unit
def test_invoke_structured_falls_back_when_result_is_none():
    """구조화 결과가 None이면 자유 텍스트로 폴백하는지 검증하는 테스트."""
    # 사고(thinking) 모델은 일반 텍스트로만 답해 파서에 None을 남길 수 있다.
    # 그 경우 render(None)에서 크래시하지 말고 자유 텍스트로 폴백해야 한다 (#1051).
    from tradingagents.agents.utils.structured import invoke_structured_or_freetext

    structured = MagicMock()
    structured.invoke.return_value = None
    plain = MagicMock()
    plain.invoke.return_value = MagicMock(content="FREETEXT")

    out = invoke_structured_or_freetext(
        structured, plain, "prompt", render=lambda r: r.rating, agent_name="t"
    )
    assert out == "FREETEXT"
    plain.invoke.assert_called_once()


@pytest.mark.unit
class TestTraderAgent:
    def test_structured_path_produces_rendered_markdown(self):
        """구조화 경로가 렌더링된 마크다운 투자 계획을 생성하는지 검증하는 테스트."""
        captured = {}
        proposal = TraderProposal(
            action=TraderAction.BUY,
            reasoning="AI capex cycle intact; institutional flows constructive.",
            entry_price=189.5,
            stop_loss=178.0,
            position_sizing="6% of portfolio",
        )
        llm = _structured_trader_llm(captured, proposal)
        trader = create_trader(llm)
        result = trader(_make_trader_state())
        plan = result["trader_investment_plan"]
        assert "**Action**: Buy" in plan
        assert "**Entry Price**: 189.5" in plan
        assert "FINAL TRANSACTION PROPOSAL: **BUY**" in plan
        # 동일한 렌더링 마크다운이 하류(downstream) 에이전트를 위해 messages에도 추가된다.
        assert plan in result["messages"][0].content

    def test_prompt_includes_investment_plan(self):
        """트레이더 프롬프트에 투자 계획이 포함되는지 검증하는 테스트."""
        captured = {}
        llm = _structured_trader_llm(captured)
        trader = create_trader(llm)
        trader(_make_trader_state())
        # 투자 계획은 캡처된 프롬프트의 사용자 메시지에 들어 있다.
        prompt = captured["prompt"]
        assert any("Proposed Investment Plan" in m["content"] for m in prompt)

    def test_falls_back_to_freetext_when_structured_unavailable(self):
        """구조화 출력이 지원되지 않을 때 자유 텍스트로 폴백하는지 검증하는 테스트."""
        plain_response = (
            "**Action**: Sell\n\nGuidance cut hits margins.\n\n"
            "FINAL TRANSACTION PROPOSAL: **SELL**"
        )
        llm = MagicMock()
        llm.with_structured_output.side_effect = NotImplementedError("provider unsupported")
        llm.invoke.return_value = MagicMock(content=plain_response)
        trader = create_trader(llm)
        result = trader(_make_trader_state())
        assert result["trader_investment_plan"] == plain_response


# ---------------------------------------------------------------------------
# 리서치 매니저 에이전트: 구조화 정상 경로 + 폴백
# ---------------------------------------------------------------------------


def _make_rm_state():
    return {
        "company_of_interest": "NVDA",
        "investment_debate_state": {
            "history": "Bull and bear arguments here.",
            "bull_history": "Bull says...",
            "bear_history": "Bear says...",
            "current_response": "",
            "judge_decision": "",
            "count": 1,
        },
    }


def _structured_rm_llm(captured: dict, plan: ResearchPlan | None = None):
    if plan is None:
        # bull/bear_case_assessment는 중기 로드맵 #3에서 추가된 필수 필드.
        plan = ResearchPlan(
            recommendation=PortfolioRating.HOLD,
            bull_case_assessment="Bull evidence is grounded but incomplete.",
            bear_case_assessment="Bear raised valid risks, partially answered.",
            rationale="Balanced view across both sides.",
            strategic_actions="Hold current position; reassess after earnings.",
        )
    structured = MagicMock()
    structured.invoke.side_effect = lambda prompt: (
        captured.__setitem__("prompt", prompt) or plan
    )
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    return llm


@pytest.mark.unit
class TestResearchManagerAgent:
    def test_structured_path_produces_rendered_markdown(self):
        """구조화 경로가 렌더링된 마크다운 투자 계획을 생성하는지 검증하는 테스트."""
        captured = {}
        plan = ResearchPlan(
            recommendation=PortfolioRating.OVERWEIGHT,
            bull_case_assessment="Bull's AI-demand claim is backed by the market report.",
            bear_case_assessment="Bear's valuation concern went unanswered but is secondary.",
            rationale="Bull case is stronger; AI tailwind intact.",
            strategic_actions="Build position gradually over two weeks.",
        )
        llm = _structured_rm_llm(captured, plan)
        rm = create_research_manager(llm)
        result = rm(_make_rm_state())
        ip = result["investment_plan"]
        assert "**Recommendation**: Overweight" in ip
        assert "**Rationale**: Bull case" in ip
        assert "**Strategic Actions**: Build position" in ip

    def test_prompt_uses_5_tier_rating_scale(self):
        """리서치 매니저 프롬프트에 5단계 등급이 모두 나열되는지 검증하는 테스트 (스키마 열거형과 사용자 기대의 일치)."""
        captured = {}
        llm = _structured_rm_llm(captured)
        rm = create_research_manager(llm)
        rm(_make_rm_state())
        prompt = captured["prompt"]
        for tier in ("Buy", "Overweight", "Hold", "Underweight", "Sell"):
            assert f"**{tier}**" in prompt, f"missing {tier} in prompt"

    def test_falls_back_to_freetext_when_structured_unavailable(self):
        """구조화 출력이 지원되지 않을 때 자유 텍스트로 폴백하는지 검증하는 테스트."""
        plain_response = "**Recommendation**: Sell\n\n**Rationale**: ...\n\n**Strategic Actions**: ..."
        llm = MagicMock()
        llm.with_structured_output.side_effect = NotImplementedError("provider unsupported")
        llm.invoke.return_value = MagicMock(content=plain_response)
        rm = create_research_manager(llm)
        result = rm(_make_rm_state())
        assert result["investment_plan"] == plain_response


# ---------------------------------------------------------------------------
# 감성 분석가: 스키마, 렌더링, 구조화 정상 경로 + 폴백
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRenderSentimentReport:
    def test_header_contains_band_and_score(self):
        """보고서 헤더에 감성 밴드(band)와 점수가 포함되는지 검증하는 테스트."""
        report = SentimentReport(
            overall_band=SentimentBand.BULLISH,
            overall_score=7.2,
            confidence="high",
            narrative="Source breakdown here.",
        )
        md = render_sentiment_report(report)
        assert "**Overall Sentiment:** **Bullish**" in md
        assert "(Score: 7.2/10)" in md

    def test_header_contains_confidence(self):
        """보고서 헤더에 신뢰도(confidence)가 표시되는지 검증하는 테스트."""
        report = SentimentReport(
            overall_band=SentimentBand.NEUTRAL,
            overall_score=5.0,
            confidence="low",
            narrative="Limited data.",
        )
        assert "**Confidence:** Low" in render_sentiment_report(report)

    def test_narrative_preserved_in_output(self):
        """서술(narrative) 본문이 마크다운 형식 그대로 출력에 보존되는지 검증하는 테스트."""
        narrative = "## Breakdown\n\nStockTwits: 70% bullish.\n\n| Signal | Direction |\n|---|---|\n| News | Neutral |"
        report = SentimentReport(
            overall_band=SentimentBand.MILDLY_BULLISH,
            overall_score=6.0,
            confidence="medium",
            narrative=narrative,
        )
        assert narrative in render_sentiment_report(report)

    def test_all_six_bands_render(self):
        """여섯 가지 감성 밴드 전부가 렌더링되는지 검증하는 테스트."""
        for band in SentimentBand:
            report = SentimentReport(
                overall_band=band, overall_score=5.0,
                confidence="medium", narrative="n",
            )
            assert band.value in render_sentiment_report(report)

    def test_score_out_of_range_rejected(self):
        """범위를 벗어난 점수가 검증 오류(ValidationError)로 거부되는지 검증하는 테스트."""
        with pytest.raises(ValidationError):
            SentimentReport(
                overall_band=SentimentBand.BULLISH, overall_score=11.0,
                confidence="high", narrative="n",
            )


def _make_sentiment_state():
    return {
        "company_of_interest": "NVDA",
        "trade_date": "2026-01-15",
        "asset_type": "stock",
        "messages": [],
    }


def _structured_sentiment_llm(captured: dict, report: SentimentReport | None = None):
    """구조화 바인딩이 프롬프트를 캡처하고 실제 SentimentReport를 반환하는
    MagicMock LLM을 생성한다 (render_sentiment_report가 동작하도록)."""
    if report is None:
        report = SentimentReport(
            overall_band=SentimentBand.BULLISH, overall_score=7.5,
            confidence="high",
            narrative="StockTwits 75% bullish. News constructive. Reddit upbeat.",
        )
    structured = MagicMock()
    structured.invoke.side_effect = lambda prompt: (
        captured.__setitem__("prompt", prompt) or report
    )
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    return llm


@pytest.mark.unit
class TestSentimentAnalystAgent:
    def test_structured_path_produces_rendered_markdown(self):
        """구조화 경로가 렌더링된 마크다운 감성 보고서를 생성하는지 검증하는 테스트."""
        captured = {}
        report = SentimentReport(
            overall_band=SentimentBand.MILDLY_BEARISH, overall_score=4.0,
            confidence="medium", narrative="Mixed signals across sources.",
        )
        analyst = create_sentiment_analyst(_structured_sentiment_llm(captured, report))
        sr = analyst(_make_sentiment_state())["sentiment_report"]
        assert "**Overall Sentiment:** **Mildly Bearish**" in sr
        assert "(Score: 4.0/10)" in sr
        assert "Mixed signals across sources." in sr

    def test_sentiment_report_also_in_messages(self):
        """감성 보고서가 messages에도 동일하게 포함되는지 검증하는 테스트."""
        captured = {}
        analyst = create_sentiment_analyst(_structured_sentiment_llm(captured))
        result = analyst(_make_sentiment_state())
        assert len(result["messages"]) == 1
        assert result["sentiment_report"] == result["messages"][0].content

    def test_prompt_contains_ticker(self):
        """프롬프트에 대상 티커(NVDA)가 포함되는지 검증하는 테스트."""
        captured = {}
        create_sentiment_analyst(_structured_sentiment_llm(captured))(_make_sentiment_state())
        assert any("NVDA" in str(m) for m in captured["prompt"])

    def test_falls_back_to_freetext_when_structured_unavailable(self):
        """구조화 출력이 지원되지 않을 때 자유 텍스트로 폴백하는지 검증하는 테스트."""
        plain = "**Overall Sentiment:** **Bearish** (Score: 3.0/10)\n**Confidence:** Low\n\nLimited data."
        llm = MagicMock()
        llm.with_structured_output.side_effect = NotImplementedError("provider unsupported")
        llm.invoke.return_value = MagicMock(content=plain)
        assert create_sentiment_analyst(llm)(_make_sentiment_state())["sentiment_report"] == plain

    def test_falls_back_to_freetext_when_structured_call_fails(self):
        """구조화 호출 자체가 실패해도 자유 텍스트로 폴백하는지 검증하는 테스트."""
        plain = "Fallback free-text sentiment."
        structured = MagicMock()
        structured.invoke.side_effect = ValueError("bad JSON from model")
        llm = MagicMock()
        llm.with_structured_output.return_value = structured
        llm.invoke.return_value = MagicMock(content=plain)
        assert create_sentiment_analyst(llm)(_make_sentiment_state())["sentiment_report"] == plain


# ---------------------------------------------------------------------------
# 자유 텍스트 폴백의 영어 등급 줄 강제 (설계분석 단기 로드맵 #1)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFallbackRatingLine:
    """require_rating_line이 폴백 프롬프트에 영어 등급 지시를 덧붙이는지 검증.

    구조화 출력 실패 + 한국어 출력 언어 조합에서 등급 파서가 기본값(Hold)에
    고착되는 것을 막는 안전장치다.
    """

    def _failing_structured(self):
        structured = MagicMock()
        structured.invoke.side_effect = ValueError("provider glitch")
        return structured

    def test_fallback_prompt_gets_rating_instruction(self):
        """폴백 시 문자열 프롬프트 끝에 등급 지시문이 붙는지 검증하는 테스트."""
        from tradingagents.agents.utils.structured import (
            RATING_LINE_INSTRUCTION,
            invoke_structured_or_freetext,
        )

        plain = MagicMock()
        plain.invoke.return_value = MagicMock(content="자유 텍스트 결정문\n\nRating: Sell")

        out = invoke_structured_or_freetext(
            self._failing_structured(), plain, "원래 프롬프트",
            render=lambda r: "unused", agent_name="PM",
            require_rating_line=True,
        )
        sent_prompt = plain.invoke.call_args[0][0]
        assert sent_prompt.startswith("원래 프롬프트")
        assert sent_prompt.endswith(RATING_LINE_INSTRUCTION)
        assert out.endswith("Rating: Sell")

    def test_message_list_prompt_gets_extra_user_message(self):
        """메시지 리스트 프롬프트에는 지시가 별도 user 메시지로 추가되는지 검증."""
        from tradingagents.agents.utils.structured import invoke_structured_or_freetext

        plain = MagicMock()
        plain.invoke.return_value = MagicMock(content="ok")
        messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]

        invoke_structured_or_freetext(
            self._failing_structured(), plain, messages,
            render=lambda r: "unused", agent_name="PM",
            require_rating_line=True,
        )
        sent = plain.invoke.call_args[0][0]
        assert len(sent) == 3 and sent[2]["role"] == "user"
        assert "Rating:" in sent[2]["content"]
        # 원본 리스트는 변형되지 않아야 한다
        assert len(messages) == 2

    def test_structured_success_path_unmodified(self):
        """구조화 경로가 성공하면 프롬프트에 지시가 붙지 않는지 검증하는 테스트."""
        from tradingagents.agents.utils.structured import invoke_structured_or_freetext

        structured = MagicMock()
        structured.invoke.return_value = MagicMock()
        plain = MagicMock()

        invoke_structured_or_freetext(
            structured, plain, "원래 프롬프트",
            render=lambda r: "rendered", agent_name="PM",
            require_rating_line=True,
        )
        sent_prompt = structured.invoke.call_args[0][0]
        assert sent_prompt == "원래 프롬프트"
        plain.invoke.assert_not_called()

    def test_korean_fallback_decision_extracts_correct_signal(self):
        """통합 시나리오: 한국어 자유 텍스트 폴백에서 Sell이 Sell로 추출되는지 검증.

        (1) 모델이 지시를 따라 영어 Rating 줄을 붙인 경우와
        (2) 지시를 무시하고 한국어 등급 라벨만 쓴 경우 모두 커버한다.
        """
        from tradingagents.agents.utils.rating import parse_rating
        from tradingagents.agents.utils.structured import invoke_structured_or_freetext

        obedient = "시장 과열로 매도가 타당합니다. (상세 근거...)\n\nRating: Sell"
        disobedient = "**등급**: 매도\n\n**요약**: 포지션 청산을 권고합니다."

        for content in (obedient, disobedient):
            plain = MagicMock()
            plain.invoke.return_value = MagicMock(content=content)
            decision = invoke_structured_or_freetext(
                self._failing_structured(), plain, "프롬프트",
                render=lambda r: "unused", agent_name="PM",
                require_rating_line=True,
            )
            assert parse_rating(decision) == "Sell"

    def test_portfolio_manager_opts_in(self):
        """포트폴리오 매니저 호출부가 require_rating_line=True를 쓰는지 검증.

        PM 출력만 시그널 파서·메모리 태그로 소비되므로 PM은 반드시 옵트인해야
        한다. 리팩터링으로 조용히 빠지는 것을 소스 검사로 방지한다.
        """
        from pathlib import Path

        src = (Path(__file__).resolve().parents[1] / "tradingagents" / "agents"
               / "managers" / "portfolio_manager.py").read_text(encoding="utf-8")
        assert "require_rating_line=True" in src
