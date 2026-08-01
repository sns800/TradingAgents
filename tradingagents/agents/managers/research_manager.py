# ============================================================================
# 리서치 매니저(Research Manager) 모듈
#
# 이 에이전트는 강세론자(Bull)/약세론자(Bear) 리서처들의 토론을 비판적으로
# 평가하고, 그 결과를 트레이더가 실행할 수 있는 구조화된 투자 계획
# (investment_plan)으로 정리하는 역할을 합니다.
# 전체 파이프라인(분석가 → 리서처 토론 → 트레이더 → 리스크 토론 → 포트폴리오 매니저)에서
# 리서처 토론 단계의 판정자(Judge)로 위치하며, 여기서 만든 투자 계획은
# 곧바로 트레이더의 거래 제안 작성에 입력됩니다.
# ============================================================================

"""리서치 매니저(Research Manager): 강세/약세 토론을 트레이더용 구조화된 투자 계획으로 변환합니다."""

from __future__ import annotations

from tradingagents.agents.schemas import ResearchPlan, render_research_plan
from tradingagents.agents.utils.agent_utils import (
    get_instrument_context_from_state,
    get_language_instruction,
)
from tradingagents.agents.utils.structured import (
    NO_EXTERNAL_TOOLS,
    bind_structured,
    invoke_structured_or_freetext,
)


def create_research_manager(llm):
    # 구조화 출력 바인딩: LLM이 ResearchPlan 스키마 형태로 응답하도록 감쌉니다.
    structured_llm = bind_structured(llm, ResearchPlan, "Research Manager")

    # LangGraph 노드 함수: 상태(state) 딕셔너리를 입력받아
    # 갱신할 키들만 담은 딕셔너리를 반환합니다.
    def research_manager_node(state) -> dict:
        instrument_context = get_instrument_context_from_state(state)  # 종목/자산 정보 문자열
        # 강세론자(Bull)/약세론자(Bear) 토론의 전체 이력을 상태에서 꺼냅니다.
        history = state["investment_debate_state"].get("history", "")

        investment_debate_state = state["investment_debate_state"]

        # 분석가 4종 원본 보고서: 심판이 토론자들의 주장을 원자료와 대조해
        # 검증할 수 있도록 리스크 토론자와 동일한 방식으로 제공합니다.
        # 분석가 일부만 선택된 실행에서는 키가 없거나 빈 문자열일 수 있으므로
        # .get()으로 안전하게 꺼냅니다.
        market_research_report = state.get("market_report", "")
        sentiment_report = state.get("sentiment_report", "")
        news_report = state.get("news_report", "")
        fundamentals_report = state.get("fundamentals_report", "")

        # [한국어 요약] 아래 f-string 프롬프트는 LLM에게 다음을 지시합니다:
        # "리서치 매니저이자 토론 진행자로서 이번 토론 라운드를 비판적으로 평가하고,
        # 트레이더를 위한 명확하고 실행 가능한 투자 계획을 제시하라.
        # 등급(Rating)은 Buy(매수)/Overweight(비중 확대)/Hold(보유)/
        # Underweight(비중 축소)/Sell(매도) 중 정확히 하나를 사용하라.
        # 토론의 가장 강한 논거가 뒷받침될 때는 분명한 입장을 취하고,
        # Hold는 양측 근거가 진정으로 균형일 때만 남겨 두라.
        # 분석가 원본 보고서 4종과 토론 이력이 컨텍스트로 주어진다.
        # 토론자의 주장은 원본 보고서와 대조해 검증하고, 보고서에 근거가 없는
        # 주장은 낮게 평가하라 (해당 분석가가 실행되지 않았으면 보고서가
        # 비어 있을 수 있다). 외부 도구는 사용하지 말라."
        # ※ 프롬프트를 번역하면 모델 출력 형식이 깨질 수 있어 영어 원문을 유지합니다.
        prompt = f"""As the Research Manager and debate facilitator, your role is to critically evaluate this round of debate and deliver a clear, actionable investment plan for the trader.

{instrument_context}

---

**Rating Scale** (use exactly one):
- **Buy**: Strong conviction in the bull thesis; recommend taking or growing the position
- **Overweight**: Constructive view; recommend gradually increasing exposure
- **Hold**: Balanced view; recommend maintaining the current position
- **Underweight**: Cautious view; recommend trimming exposure
- **Sell**: Strong conviction in the bear thesis; recommend exiting or avoiding the position

Commit to a clear stance whenever the debate's strongest arguments warrant one; reserve Hold for situations where the evidence on both sides is genuinely balanced.

---

**Analyst Reports** (original evidence — cross-check the debaters' claims against these reports and discount claims they do not support; a report may be empty if that analyst was not run):
Market Research Report: {market_research_report}
Social Media Sentiment Report: {sentiment_report}
Latest World Affairs Report: {news_report}
Company Fundamentals Report: {fundamentals_report}

---

**Debate History:**
{history}

{NO_EXTERNAL_TOOLS}""" + get_language_instruction()

        # 구조화 출력을 우선 시도하고, 지원하지 않는 제공자(provider)에서는
        # 자유 텍스트 생성으로 대체(fallback)하여 투자 계획 텍스트를 얻습니다.
        investment_plan = invoke_structured_or_freetext(
            structured_llm,
            llm,
            prompt,
            render_research_plan,
            "Research Manager",
        )

        # 투자 토론 상태(investment_debate_state)를 갱신합니다: 투자 계획을
        # "judge_decision"(판정 결과)과 "current_response"에 기록하고,
        # 기존 강세/약세 토론 이력은 그대로 보존합니다.
        # ※ dict 키 이름은 프로그램 동작에 쓰이므로 절대 변경하면 안 됩니다.
        new_investment_debate_state = {
            "judge_decision": investment_plan,
            "history": investment_debate_state.get("history", ""),
            "bear_history": investment_debate_state.get("bear_history", ""),
            "bull_history": investment_debate_state.get("bull_history", ""),
            "current_response": investment_plan,
            "count": investment_debate_state["count"],
        }

        # 상태(state) 갱신: 갱신된 토론 상태와 투자 계획을 반환합니다.
        # investment_plan은 다음 단계인 트레이더 노드가 사용합니다.
        return {
            "investment_debate_state": new_investment_debate_state,
            "investment_plan": investment_plan,
        }

    return research_manager_node
