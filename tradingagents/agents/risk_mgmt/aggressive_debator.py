# =============================================================================
# [모듈 개요 - 초보자용]
# 공격적 리스크 애널리스트(Aggressive Risk Analyst) 에이전트입니다. 트레이더의
# 매매 결정을 놓고 "고위험·고수익 기회를 놓치지 말자"는 입장에서 보수적/중립적
# 애널리스트의 신중론을 반박합니다. 전체 파이프라인 「분석가 → 리서처 토론 →
# 트레이더 → 리스크 토론 → 포트폴리오 매니저」 중 네 번째 단계인 리스크 토론에서
# 공격적(Aggressive) 관점을 담당하며, 3자 토론 결과는 포트폴리오 매니저가
# 최종 판단에 활용합니다.
# =============================================================================

from tradingagents.agents.utils.agent_utils import (
    get_instrument_context_from_state,
    get_language_instruction,
)


def create_aggressive_debator(llm):
    # LangGraph 그래프에 노드(node)로 등록될 함수를 만들어 돌려주는 팩토리입니다.
    # 반환된 aggressive_node는 현재 상태(state) dict를 받아, 갱신할 키만 담은
    # dict를 돌려주고 LangGraph가 이를 기존 상태에 병합(merge)합니다.
    def aggressive_node(state) -> dict:
        # risk_debate_state: 공격적/보수적/중립적 3자 리스크 토론의 진행 상황을
        # 담는 하위 상태. history는 전체 토론 기록, aggressive_history는
        # 공격적 애널리스트 발언만 모은 기록입니다.
        risk_debate_state = state["risk_debate_state"]
        history = risk_debate_state.get("history", "")
        aggressive_history = risk_debate_state.get("aggressive_history", "")

        # 반박 대상인 다른 두 애널리스트(보수적/중립적)의 최근 발언입니다.
        current_conservative_response = risk_debate_state.get("current_conservative_response", "")
        current_neutral_response = risk_debate_state.get("current_neutral_response", "")

        market_research_report = state["market_report"]
        sentiment_report = state["sentiment_report"]
        news_report = state["news_report"]
        fundamentals_report = state["fundamentals_report"]
        instrument_context = get_instrument_context_from_state(state)

        # 이전 단계에서 트레이더(Trader)가 내놓은 매매 결정. 이 토론의 심사 대상입니다.
        trader_decision = state["trader_investment_plan"]

        # [프롬프트 요약 - 한국어] 공격적 리스크 애널리스트 역할 지시문:
        # 트레이더의 결정에 대해 상방 잠재력·성장성·혁신 이점을 강조하고,
        # 보수적/중립적 애널리스트의 각 논점에 데이터 기반으로 반박하며,
        # 고위험 접근이 최선인 이유를 대화체(서식 없이)로 설득하라는 내용.
        # 아래에 4종 분석 보고서와 토론 이력을 근거 자료로 제공합니다.
        # (LLM 프롬프트이므로 영어 원문 유지)
        prompt = f"""As the Aggressive Risk Analyst, your role is to actively champion high-reward, high-risk opportunities, emphasizing bold strategies and competitive advantages. When evaluating the trader's decision or plan, focus intently on the potential upside, growth potential, and innovative benefits—even when these come with elevated risk. Use the provided market data and sentiment analysis to strengthen your arguments and challenge the opposing views. Specifically, respond directly to each point made by the conservative and neutral analysts, countering with data-driven rebuttals and persuasive reasoning. Highlight where their caution might miss critical opportunities or where their assumptions may be overly conservative. Here is the trader's decision:

{trader_decision}

Your task is to create a compelling case for the trader's decision by questioning and critiquing the conservative and neutral stances to demonstrate why your high-reward perspective offers the best path forward. Incorporate insights from the following sources into your arguments:

{instrument_context}
Market Research Report: {market_research_report}
Social Media Sentiment Report: {sentiment_report}
Latest World Affairs Report: {news_report}
Company Fundamentals Report: {fundamentals_report}
Here is the current conversation history: {history} Here are the last arguments from the conservative analyst: {current_conservative_response} Here are the last arguments from the neutral analyst: {current_neutral_response}. If there are no responses from the other viewpoints yet, present your own argument based on the available data.

Engage actively by addressing any specific concerns raised, refuting the weaknesses in their logic, and asserting the benefits of risk-taking to outpace market norms. Maintain a focus on debating and persuading, not just presenting data. Challenge each counterpoint to underscore why a high-risk approach is optimal. Output conversationally as if you are speaking without any special formatting.""" + get_language_instruction()

        # LLM을 한 번 호출해 공격적 애널리스트의 발언을 생성합니다.
        response = llm.invoke(prompt)

        # 발언 앞에 화자 라벨을 붙입니다. (토론 기록에서 누구 발언인지 구분용)
        argument = f"Aggressive Analyst: {response.content}"

        # 토론 상태를 새로 만들어 돌려줍니다. history와 aggressive_history에는
        # 이번 발언을 덧붙이고, latest_speaker와 current_aggressive_response를
        # 갱신해 다음 화자가 이 발언에 반박하게 하며, 다른 두 애널리스트의 기록은
        # 그대로 보존합니다. count(발언 횟수)는 토론 종료 조건 판단에 쓰입니다.
        new_risk_debate_state = {
            "history": history + "\n" + argument,
            "aggressive_history": aggressive_history + "\n" + argument,
            "conservative_history": risk_debate_state.get("conservative_history", ""),
            "neutral_history": risk_debate_state.get("neutral_history", ""),
            "latest_speaker": "Aggressive",
            "current_aggressive_response": argument,
            "current_conservative_response": risk_debate_state.get("current_conservative_response", ""),
            "current_neutral_response": risk_debate_state.get(
                "current_neutral_response", ""
            ),
            "count": risk_debate_state["count"] + 1,
        }

        # LangGraph 규칙: 갱신하려는 상태 키만 담은 dict를 반환하면
        # 프레임워크가 전체 상태에 병합해 줍니다.
        return {"risk_debate_state": new_risk_debate_state}

    return aggressive_node
