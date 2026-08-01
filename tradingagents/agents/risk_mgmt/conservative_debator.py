# =============================================================================
# [모듈 개요 - 초보자용]
# 보수적 리스크 애널리스트(Conservative Risk Analyst) 에이전트입니다. 트레이더의
# 매매 결정을 놓고 "자산 보호와 변동성 최소화가 우선"이라는 입장에서 공격적/
# 중립적 애널리스트의 낙관론을 반박합니다. 전체 파이프라인 「분석가 → 리서처
# 토론 → 트레이더 → 리스크 토론 → 포트폴리오 매니저」 중 네 번째 단계인 리스크
# 토론에서 보수적(Conservative) 관점을 담당하며, 3자 토론 결과는 포트폴리오
# 매니저가 최종 판단에 활용합니다.
# =============================================================================

from tradingagents.agents.utils.agent_utils import (
    get_instrument_context_from_state,
    get_language_instruction,
)
from tradingagents.agents.utils.debate_context import condense_debate_history


def create_conservative_debator(llm):
    # LangGraph 그래프에 노드(node)로 등록될 함수를 만들어 돌려주는 팩토리입니다.
    # 반환된 conservative_node는 현재 상태(state) dict를 받아, 갱신할 키만 담은
    # dict를 돌려주고 LangGraph가 이를 기존 상태에 병합(merge)합니다.
    def conservative_node(state) -> dict:
        # risk_debate_state: 공격적/보수적/중립적 3자 리스크 토론의 진행 상황을
        # 담는 하위 상태. history는 전체 토론 기록, conservative_history는
        # 보수적 애널리스트 발언만 모은 기록입니다.
        risk_debate_state = state["risk_debate_state"]
        history = risk_debate_state.get("history", "")
        # 프롬프트용 압축 이력: 직전 발언은 전문, 그 이전 발언들은 각 300자
        # 절단 (토큰 O(라운드²) 완화 — 중기 로드맵 #3). 상태에 저장되는
        # history 원본은 그대로 유지되며, 심판(PM)은 전체 이력을 받는다.
        condensed_history = condense_debate_history(history)
        conservative_history = risk_debate_state.get("conservative_history", "")

        # 반박 대상인 다른 두 애널리스트(공격적/중립적)의 최근 발언입니다.
        current_aggressive_response = risk_debate_state.get("current_aggressive_response", "")
        current_neutral_response = risk_debate_state.get("current_neutral_response", "")

        market_research_report = state["market_report"]
        sentiment_report = state["sentiment_report"]
        news_report = state["news_report"]
        fundamentals_report = state["fundamentals_report"]
        instrument_context = get_instrument_context_from_state(state)

        # 이전 단계에서 트레이더(Trader)가 내놓은 매매 결정. 이 토론의 심사 대상입니다.
        trader_decision = state["trader_investment_plan"]

        # [프롬프트 요약 - 한국어] 보수적 리스크 애널리스트 역할 지시문:
        # 자산 보호·변동성 최소화·안정적 성장을 최우선으로 트레이더 결정의
        # 고위험 요소를 비판적으로 점검하고, 공격적/중립적 애널리스트의 논점에
        # 직접 반박하며, 저위험 전략이 최선인 이유를 대화체(서식 없이)로
        # 설득하라는 내용. 아래에 4종 분석 보고서와 토론 이력을 근거 자료로
        # 제공합니다. (LLM 프롬프트이므로 영어 원문 유지)
        prompt = f"""As the Conservative Risk Analyst, your primary objective is to protect assets, minimize volatility, and ensure steady, reliable growth. You prioritize stability, security, and risk mitigation, carefully assessing potential losses, economic downturns, and market volatility. When evaluating the trader's decision or plan, critically examine high-risk elements, pointing out where the decision may expose the firm to undue risk and where more cautious alternatives could secure long-term gains. Here is the trader's decision:

{trader_decision}

Your task is to actively counter the arguments of the Aggressive and Neutral Analysts, highlighting where their views may overlook potential threats or fail to prioritize sustainability. Respond directly to their points, drawing from the following data sources to build a convincing case for a low-risk approach adjustment to the trader's decision:

{instrument_context}
Market Research Report: {market_research_report}
Social Media Sentiment Report: {sentiment_report}
Latest World Affairs Report: {news_report}
Company Fundamentals Report: {fundamentals_report}
Here is the current conversation history (earlier arguments are truncated for brevity; the latest argument is shown in full): {condensed_history} Here is the last response from the aggressive analyst: {current_aggressive_response} Here is the last response from the neutral analyst: {current_neutral_response}. If there are no responses from the other viewpoints yet, present your own argument based on the available data.

Engage by questioning their optimism and emphasizing the potential downsides they may have overlooked. Address each of their counterpoints to showcase why a conservative stance is ultimately the safest path for the firm's assets. Focus on debating and critiquing their arguments to demonstrate the strength of a low-risk strategy over their approaches. Output conversationally as if you are speaking without any special formatting.""" + get_language_instruction()

        # LLM을 한 번 호출해 보수적 애널리스트의 발언을 생성합니다.
        response = llm.invoke(prompt)

        # 발언 앞에 화자 라벨을 붙입니다. (토론 기록에서 누구 발언인지 구분용)
        argument = f"Conservative Analyst: {response.content}"

        # 토론 상태를 새로 만들어 돌려줍니다. history와 conservative_history에는
        # 이번 발언을 덧붙이고, latest_speaker와 current_conservative_response를
        # 갱신해 다음 화자가 이 발언에 반박하게 하며, 다른 두 애널리스트의 기록은
        # 그대로 보존합니다. count(발언 횟수)는 토론 종료 조건 판단에 쓰입니다.
        new_risk_debate_state = {
            "history": history + "\n" + argument,
            "aggressive_history": risk_debate_state.get("aggressive_history", ""),
            "conservative_history": conservative_history + "\n" + argument,
            "neutral_history": risk_debate_state.get("neutral_history", ""),
            "latest_speaker": "Conservative",
            "current_aggressive_response": risk_debate_state.get(
                "current_aggressive_response", ""
            ),
            "current_conservative_response": argument,
            "current_neutral_response": risk_debate_state.get(
                "current_neutral_response", ""
            ),
            "count": risk_debate_state["count"] + 1,
        }

        # LangGraph 규칙: 갱신하려는 상태 키만 담은 dict를 반환하면
        # 프레임워크가 전체 상태에 병합해 줍니다.
        return {"risk_debate_state": new_risk_debate_state}

    return conservative_node
