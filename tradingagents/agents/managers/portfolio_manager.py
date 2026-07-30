# ============================================================================
# 포트폴리오 매니저(Portfolio Manager) 모듈
#
# 이 에이전트는 공격적(Aggressive)/보수적(Conservative)/중립적(Neutral)
# 리스크 분석가들의 토론을 종합해 최종 거래 결정(final_trade_decision)을
# 내리는 역할을 합니다.
# 전체 파이프라인(분석가 → 리서처 토론 → 트레이더 → 리스크 토론 → 포트폴리오 매니저)에서
# 가장 마지막 단계에 위치하며, 리서치 매니저의 투자 계획과 트레이더의 거래 제안,
# 리스크 토론 이력을 모두 입력받아 최종 판정(Judge) 결과를 산출합니다.
# ============================================================================

"""포트폴리오 매니저(Portfolio Manager): 리스크 분석가 토론을 종합해 최종 결정을 내립니다.

LangChain의 ``with_structured_output`` 을 사용해 LLM이 단 한 번의 호출로
타입이 지정된 ``PortfolioDecision`` 을 직접 생성하게 합니다. 결과는
``final_trade_decision`` 에 저장하기 위해 다시 마크다운으로 렌더링되므로,
메모리 로그, CLI 표시, 저장된 보고서가 지금과 동일한 형태를 계속 사용할 수
있습니다. 제공자(provider)가 구조화 출력을 지원하지 않는 경우에는
자유 텍스트 생성으로 우아하게 대체(fallback)됩니다.
"""

from __future__ import annotations

from tradingagents.agents.schemas import PortfolioDecision, render_pm_decision
from tradingagents.agents.utils.agent_utils import (
    get_instrument_context_from_state,
    get_language_instruction,
)
from tradingagents.agents.utils.structured import (
    NO_EXTERNAL_TOOLS,
    bind_structured,
    invoke_structured_or_freetext,
)


def create_portfolio_manager(llm):
    # 구조화 출력 바인딩: LLM이 PortfolioDecision 스키마 형태로 응답하도록 감쌉니다.
    structured_llm = bind_structured(llm, PortfolioDecision, "Portfolio Manager")

    # LangGraph 노드 함수: 상태(state) 딕셔너리를 입력받아
    # 갱신할 키들만 담은 딕셔너리를 반환합니다.
    def portfolio_manager_node(state) -> dict:
        instrument_context = get_instrument_context_from_state(state)  # 종목/자산 정보 문자열

        # 상태에서 리스크 토론 이력과 상위 단계 산출물들을 꺼냅니다.
        history = state["risk_debate_state"]["history"]  # 리스크 분석가 토론 전체 이력
        risk_debate_state = state["risk_debate_state"]
        research_plan = state["investment_plan"]  # 리서치 매니저의 투자 계획
        trader_plan = state["trader_investment_plan"]  # 트레이더의 거래 제안

        # 과거 결정과 결과에서 얻은 교훈(메모리)이 있으면 프롬프트에 포함합니다.
        past_context = state.get("past_context", "")
        lessons_line = (
            f"- Lessons from prior decisions and outcomes:\n{past_context}\n"
            if past_context
            else ""
        )

        # [한국어 요약] 아래 f-string 프롬프트는 LLM에게 다음을 지시합니다:
        # "포트폴리오 매니저로서 리스크 분석가들의 토론을 종합해 최종 거래 결정을 내려라.
        # 등급(Rating)은 Buy(매수)/Overweight(비중 확대)/Hold(보유)/
        # Underweight(비중 축소)/Sell(매도) 중 정확히 하나를 사용하라.
        # 리서치 매니저의 투자 계획, 트레이더의 거래 제안, (있다면) 과거 교훈,
        # 리스크 토론 이력이 컨텍스트로 주어진다. 단호하게 결정하고
        # 모든 결론을 분석가들의 구체적 근거에 기반하라. 외부 도구는 사용하지 말라."
        # ※ 프롬프트를 번역하면 모델 출력 형식이 깨질 수 있어 영어 원문을 유지합니다.
        prompt = f"""As the Portfolio Manager, synthesize the risk analysts' debate and deliver the final trading decision.

{instrument_context}

---

**Rating Scale** (use exactly one):
- **Buy**: Strong conviction to enter or add to position
- **Overweight**: Favorable outlook, gradually increase exposure
- **Hold**: Maintain current position, no action needed
- **Underweight**: Reduce exposure, take partial profits
- **Sell**: Exit position or avoid entry

**Context:**
- Research Manager's investment plan: **{research_plan}**
- Trader's transaction proposal: **{trader_plan}**
{lessons_line}
**Risk Analysts Debate History:**
{history}

---

Be decisive and ground every conclusion in specific evidence from the analysts.

{NO_EXTERNAL_TOOLS}{get_language_instruction()}"""

        # 구조화 출력을 우선 시도하고, 지원하지 않는 제공자에서는
        # 자유 텍스트 생성으로 대체(fallback)하여 최종 결정 텍스트를 얻습니다.
        final_trade_decision = invoke_structured_or_freetext(
            structured_llm,
            llm,
            prompt,
            render_pm_decision,
            "Portfolio Manager",
        )

        # 리스크 토론 상태(risk_debate_state)를 갱신합니다: 최종 결정을
        # "judge_decision"(판정 결과)에 기록하고, 기존 토론 이력은 그대로 보존하며,
        # 마지막 발언자를 "Judge"(판정자)로 표시합니다.
        # ※ dict 키 이름은 프로그램 동작에 쓰이므로 절대 변경하면 안 됩니다.
        new_risk_debate_state = {
            "judge_decision": final_trade_decision,
            "history": risk_debate_state["history"],
            "aggressive_history": risk_debate_state["aggressive_history"],
            "conservative_history": risk_debate_state["conservative_history"],
            "neutral_history": risk_debate_state["neutral_history"],
            "latest_speaker": "Judge",
            "current_aggressive_response": risk_debate_state["current_aggressive_response"],
            "current_conservative_response": risk_debate_state["current_conservative_response"],
            "current_neutral_response": risk_debate_state["current_neutral_response"],
            "count": risk_debate_state["count"],
        }

        # 상태(state) 갱신: 갱신된 리스크 토론 상태와 최종 거래 결정을 반환합니다.
        return {
            "risk_debate_state": new_risk_debate_state,
            "final_trade_decision": final_trade_decision,
        }

    return portfolio_manager_node
