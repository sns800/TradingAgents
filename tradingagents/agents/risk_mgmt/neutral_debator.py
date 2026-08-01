# =============================================================================
# [모듈 개요 - 초보자용]
# 중립적 리스크 애널리스트(Neutral Risk Analyst) 에이전트입니다. 트레이더의
# 매매 결정을 놓고 이익과 위험을 균형 있게 저울질하며, 공격적 애널리스트의
# 지나친 낙관과 보수적 애널리스트의 지나친 신중함을 양쪽 모두 지적합니다.
# 전체 파이프라인 「분석가 → 리서처 토론 → 트레이더 → 리스크 토론 → 포트폴리오
# 매니저」 중 네 번째 단계인 리스크 토론에서 중립적(Neutral) 관점을 담당하며,
# 3자 토론 결과는 포트폴리오 매니저가 최종 판단에 활용합니다.
# =============================================================================

from tradingagents.agents.utils.agent_utils import (
    get_instrument_context_from_state,
    get_language_instruction,
    get_verified_snapshot_block,
)
from tradingagents.agents.utils.debate_context import condense_debate_history


def create_neutral_debator(llm):
    # LangGraph 그래프에 노드(node)로 등록될 함수를 만들어 돌려주는 팩토리입니다.
    # 반환된 neutral_node는 현재 상태(state) dict를 받아, 갱신할 키만 담은
    # dict를 돌려주고 LangGraph가 이를 기존 상태에 병합(merge)합니다.
    def neutral_node(state) -> dict:
        # risk_debate_state: 공격적/보수적/중립적 3자 리스크 토론의 진행 상황을
        # 담는 하위 상태. history는 전체 토론 기록, neutral_history는
        # 중립적 애널리스트 발언만 모은 기록입니다.
        risk_debate_state = state["risk_debate_state"]
        history = risk_debate_state.get("history", "")
        # 프롬프트용 압축 이력: 직전 발언은 전문, 그 이전 발언들은 각 300자
        # 절단 (토큰 O(라운드²) 완화 — 중기 로드맵 #3). 상태에 저장되는
        # history 원본은 그대로 유지되며, 심판(PM)은 전체 이력을 받는다.
        condensed_history = condense_debate_history(history)
        neutral_history = risk_debate_state.get("neutral_history", "")

        # 반박 대상인 다른 두 애널리스트(공격적/보수적)의 최근 발언입니다.
        current_aggressive_response = risk_debate_state.get("current_aggressive_response", "")
        current_conservative_response = risk_debate_state.get("current_conservative_response", "")

        market_research_report = state["market_report"]
        sentiment_report = state["sentiment_report"]
        news_report = state["news_report"]
        fundamentals_report = state["fundamentals_report"]
        instrument_context = get_instrument_context_from_state(state)
        # 검증 스냅샷 섹션(중기 로드맵 #5): 정확한 수치 인용의 기준점.
        # 비어 있으면 섹션 전체가 생략된다 (past_context 빈 값 가드와 동일 패턴).
        snapshot_block = get_verified_snapshot_block(state)

        # 이전 단계에서 트레이더(Trader)가 내놓은 매매 결정. 이 토론의 심사 대상입니다.
        trader_decision = state["trader_investment_plan"]

        # [프롬프트 요약 - 한국어] 중립적 리스크 애널리스트 역할 지시문:
        # 트레이더 결정의 이익과 위험을 균형 있게 평가하고, 공격적 애널리스트의
        # 과도한 낙관과 보수적 애널리스트의 과도한 신중함을 양쪽 모두 반박하며,
        # 중위험(균형) 전략이 최선인 이유를 대화체(서식 없이)로 설득하라는 내용.
        # 아래에 4종 분석 보고서와 토론 이력을 근거 자료로 제공합니다.
        # (LLM 프롬프트이므로 영어 원문 유지)
        prompt = f"""As the Neutral Risk Analyst, your role is to provide a balanced perspective, weighing both the potential benefits and risks of the trader's decision or plan. You prioritize a well-rounded approach, evaluating the upsides and downsides while factoring in broader market trends, potential economic shifts, and diversification strategies.Here is the trader's decision:

{trader_decision}

Your task is to challenge both the Aggressive and Conservative Analysts, pointing out where each perspective may be overly optimistic or overly cautious. Use insights from the following data sources to support a moderate, sustainable strategy to adjust the trader's decision:

{instrument_context}
Market Research Report: {market_research_report}
Social Media Sentiment Report: {sentiment_report}
Latest World Affairs Report: {news_report}
Company Fundamentals Report: {fundamentals_report}
{snapshot_block}Here is the current conversation history (earlier arguments are truncated for brevity; the latest argument is shown in full): {condensed_history} Here is the last response from the aggressive analyst: {current_aggressive_response} Here is the last response from the conservative analyst: {current_conservative_response}. If there are no responses from the other viewpoints yet, present your own argument based on the available data.

Engage actively by analyzing both sides critically, addressing weaknesses in the aggressive and conservative arguments to advocate for a more balanced approach. Challenge each of their points to illustrate why a moderate risk strategy might offer the best of both worlds, providing growth potential while safeguarding against extreme volatility. Focus on debating rather than simply presenting data, aiming to show that a balanced view can lead to the most reliable outcomes. Output conversationally as if you are speaking without any special formatting.""" + get_language_instruction()

        # LLM을 한 번 호출해 중립적 애널리스트의 발언을 생성합니다.
        response = llm.invoke(prompt)

        # 발언 앞에 화자 라벨을 붙입니다. (토론 기록에서 누구 발언인지 구분용)
        argument = f"Neutral Analyst: {response.content}"

        # 토론 상태를 새로 만들어 돌려줍니다. history와 neutral_history에는
        # 이번 발언을 덧붙이고, latest_speaker와 current_neutral_response를
        # 갱신해 다음 화자가 이 발언에 반박하게 하며, 다른 두 애널리스트의 기록은
        # 그대로 보존합니다. count(발언 횟수)는 토론 종료 조건 판단에 쓰입니다.
        new_risk_debate_state = {
            "history": history + "\n" + argument,
            "aggressive_history": risk_debate_state.get("aggressive_history", ""),
            "conservative_history": risk_debate_state.get("conservative_history", ""),
            "neutral_history": neutral_history + "\n" + argument,
            "latest_speaker": "Neutral",
            "current_aggressive_response": risk_debate_state.get(
                "current_aggressive_response", ""
            ),
            "current_conservative_response": risk_debate_state.get("current_conservative_response", ""),
            "current_neutral_response": argument,
            "count": risk_debate_state["count"] + 1,
        }

        # LangGraph 규칙: 갱신하려는 상태 키만 담은 dict를 반환하면
        # 프레임워크가 전체 상태에 병합해 줍니다.
        return {"risk_debate_state": new_risk_debate_state}

    return neutral_node
