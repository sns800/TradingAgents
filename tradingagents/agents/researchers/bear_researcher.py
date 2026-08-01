# =============================================================================
# [모듈 개요 - 초보자용]
# 약세 리서처(Bear Researcher) 에이전트입니다. "이 종목에 투자하면 안 되는 이유"
# (리스크, 경쟁 열위, 부정적 지표)를 주장하며 강세 리서처(Bull)와 토론합니다.
# 전체 파이프라인 「분석가 → 리서처 토론 → 트레이더 → 리스크 토론 → 포트폴리오
# 매니저」 중 두 번째 단계인 리서처 토론에서 매도/신중론 쪽을 담당합니다.
# 분석가 4종의 보고서를 근거로 삼고, 토론 결과는 리서치 매니저가 종합합니다.
# =============================================================================

from tradingagents.agents.utils.agent_utils import (
    get_instrument_context_from_state,
    get_language_instruction,
    get_verified_snapshot_block,
)
from tradingagents.agents.utils.debate_context import condense_debate_history


def create_bear_researcher(llm):
    # LangGraph 그래프에 노드(node)로 등록될 함수를 만들어 돌려주는 팩토리입니다.
    # 반환된 bear_node는 현재 상태(state) dict를 받아, 갱신할 키만 담은 dict를
    # 돌려줍니다. LangGraph가 이를 기존 상태에 병합(merge)해 다음 노드로 넘깁니다.
    def bear_node(state) -> dict:
        # investment_debate_state: 강세/약세 리서처 토론의 진행 상황을 담는 하위 상태.
        # history는 전체 토론 기록, bear_history는 약세론자(Bear) 발언만 모은 기록입니다.
        investment_debate_state = state["investment_debate_state"]
        history = investment_debate_state.get("history", "")
        # 프롬프트용 압축 이력: 직전 발언은 전문, 그 이전 발언들은 각 300자
        # 절단 (토큰 O(라운드²) 완화 — 중기 로드맵 #3). 상태에 저장되는
        # history 원본은 그대로 유지되며, 심판은 전체 이력을 받는다.
        condensed_history = condense_debate_history(history)
        bear_history = investment_debate_state.get("bear_history", "")

        # current_response: 직전 발언(여기서는 강세론자(Bull)의 마지막 주장).
        # 이를 프롬프트에 넣어 상대 주장에 직접 반박하게 합니다.
        current_response = investment_debate_state.get("current_response", "")
        market_research_report = state["market_report"]
        sentiment_report = state["sentiment_report"]
        news_report = state["news_report"]
        fundamentals_report = state["fundamentals_report"]
        instrument_context = get_instrument_context_from_state(state)
        # 자산 유형(주식/암호화폐 등)에 따라 프롬프트에 들어갈 표현을 고릅니다.
        # 아래 문자열들은 LLM 프롬프트에 삽입되므로 영어를 유지합니다.
        asset_type = state.get("asset_type", "stock")
        target_label = "stock" if asset_type == "stock" else "asset"
        fundamentals_label = (
            "Company fundamentals report"
            if asset_type == "stock"
            else "Asset fundamentals report (may be unavailable for crypto)"
        )
        # 검증 스냅샷 섹션(중기 로드맵 #5): 정확한 수치 인용의 기준점.
        # 비어 있으면 섹션 전체가 생략된다 (past_context 빈 값 가드와 동일 패턴).
        snapshot_block = get_verified_snapshot_block(state)

        # [프롬프트 요약 - 한국어] 약세 애널리스트(Bear Analyst) 역할 지시문:
        # 리스크·경쟁 약점·부정적 지표를 근거로 투자 반대 논리를 세우고,
        # 강세론자(Bull)의 직전 주장을 데이터로 조목조목 반박하며, 사실 나열이
        # 아닌 대화체 토론으로 응답하라는 내용. 아래에 4종 분석 보고서와 토론
        # 이력(압축본: 직전 발언 전문 + 이전 발언 300자 절단)을 근거 자료로
        # 제공합니다. 반박할 강세 주장이 아직 없으면
        # 가용 데이터에 근거한 자기 논거(개시 발언)를 제시하라는 폴백 문구를
        # 포함합니다(리스크 토론자들과 동일한 패턴). (LLM 프롬프트이므로 영어 원문 유지)
        prompt = f"""You are a Bear Analyst making the case against investing in the {target_label}. Your goal is to present a well-reasoned argument emphasizing risks, challenges, and negative indicators. Leverage the provided research and data to highlight potential downsides and counter bullish arguments effectively.

Key points to focus on:

- Risks and Challenges: Highlight factors like market saturation, financial instability, or macroeconomic threats that could hinder the stock's performance.
- Competitive Weaknesses: Emphasize vulnerabilities such as weaker market positioning, declining innovation, or threats from competitors.
- Negative Indicators: Use evidence from financial data, market trends, or recent adverse news to support your position.
- Bull Counterpoints: Critically analyze the bull argument with specific data and sound reasoning, exposing weaknesses or over-optimistic assumptions.
- Engagement: Present your argument in a conversational style, directly engaging with the bull analyst's points and debating effectively rather than simply listing facts.

Resources available:

{instrument_context}
Market research report: {market_research_report}
Social media sentiment report: {sentiment_report}
Latest world affairs news: {news_report}
{fundamentals_label}: {fundamentals_report}
{snapshot_block}Conversation history of the debate (earlier arguments are truncated for brevity; the latest argument is shown in full): {condensed_history}
Last bull argument: {current_response}
If there is no bull argument yet, this is the opening statement of the debate: present your own bear case based on the available data instead of rebutting.
Use this information to deliver a compelling bear argument, refute the bull's claims when they exist, and engage in a dynamic debate that demonstrates the risks and weaknesses of investing in the {target_label}.
""" + get_language_instruction()

        # LLM을 한 번 호출해 약세론자의 반박 발언을 생성합니다.
        response = llm.invoke(prompt)

        # 발언 앞에 화자 라벨을 붙입니다. (토론 기록에서 누구 발언인지 구분용)
        argument = f"Bear Analyst: {response.content}"

        # 토론 상태를 새로 만들어 돌려줍니다. history와 bear_history에는 이번
        # 발언을 덧붙이고, current_response를 내 발언으로 바꿔 다음 차례의
        # 강세론자(Bull)가 반박할 대상으로 삼게 하며, count(발언 횟수)를 1 올려
        # 토론 종료 조건 판단에 쓰이게 합니다.
        new_investment_debate_state = {
            "history": history + "\n" + argument,
            "bear_history": bear_history + "\n" + argument,
            "bull_history": investment_debate_state.get("bull_history", ""),
            "current_response": argument,
            "count": investment_debate_state["count"] + 1,
        }

        # LangGraph 규칙: 갱신하려는 상태 키만 담은 dict를 반환하면
        # 프레임워크가 전체 상태에 병합해 줍니다.
        return {"investment_debate_state": new_investment_debate_state}

    return bear_node
