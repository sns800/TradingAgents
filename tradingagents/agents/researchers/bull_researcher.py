# =============================================================================
# [모듈 개요 - 초보자용]
# 강세 리서처(Bull Researcher) 에이전트입니다. "이 종목에 투자해야 하는 이유"
# (성장 잠재력, 경쟁 우위, 긍정적 지표)를 주장하며 약세 리서처(Bear)와 토론합니다.
# 전체 파이프라인 「분석가 → 리서처 토론 → 트레이더 → 리스크 토론 → 포트폴리오
# 매니저」 중 두 번째 단계인 리서처 토론에서 매수/낙관론 쪽을 담당합니다.
# 분석가 4종의 보고서를 근거로 삼고, 토론 결과는 리서치 매니저가 종합합니다.
# =============================================================================

from tradingagents.agents.utils.agent_utils import (
    get_instrument_context_from_state,
    get_language_instruction,
)


def create_bull_researcher(llm):
    # LangGraph 그래프에 노드(node)로 등록될 함수를 만들어 돌려주는 팩토리입니다.
    # 반환된 bull_node는 현재 상태(state) dict를 받아, 갱신할 키만 담은 dict를
    # 돌려줍니다. LangGraph가 이를 기존 상태에 병합(merge)해 다음 노드로 넘깁니다.
    def bull_node(state) -> dict:
        # investment_debate_state: 강세/약세 리서처 토론의 진행 상황을 담는 하위 상태.
        # history는 전체 토론 기록, bull_history는 강세론자(Bull) 발언만 모은 기록입니다.
        investment_debate_state = state["investment_debate_state"]
        history = investment_debate_state.get("history", "")
        bull_history = investment_debate_state.get("bull_history", "")

        # current_response: 직전 발언(여기서는 약세론자(Bear)의 마지막 주장).
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

        # [프롬프트 요약 - 한국어] 강세 애널리스트(Bull Analyst) 역할 지시문:
        # 성장 잠재력·경쟁 우위·긍정적 지표를 근거로 투자 찬성 논리를 세우고,
        # 약세론자(Bear)의 직전 주장을 데이터로 조목조목 반박하며, 사실 나열이
        # 아닌 대화체 토론으로 응답하라는 내용. 아래에 4종 분석 보고서와 토론
        # 이력을 근거 자료로 제공합니다. (LLM 프롬프트이므로 영어 원문 유지)
        prompt = f"""You are a Bull Analyst advocating for investing in the {target_label}. Your task is to build a strong, evidence-based case emphasizing growth potential, competitive advantages, and positive market indicators. Leverage the provided research and data to address concerns and counter bearish arguments effectively.

Key points to focus on:
- Growth Potential: Highlight the company's market opportunities, revenue projections, and scalability.
- Competitive Advantages: Emphasize factors like unique products, strong branding, or dominant market positioning.
- Positive Indicators: Use financial health, industry trends, and recent positive news as evidence.
- Bear Counterpoints: Critically analyze the bear argument with specific data and sound reasoning, addressing concerns thoroughly and showing why the bull perspective holds stronger merit.
- Engagement: Present your argument in a conversational style, engaging directly with the bear analyst's points and debating effectively rather than just listing data.

Resources available:
{instrument_context}
Market research report: {market_research_report}
Social media sentiment report: {sentiment_report}
Latest world affairs news: {news_report}
{fundamentals_label}: {fundamentals_report}
Conversation history of the debate: {history}
Last bear argument: {current_response}
Use this information to deliver a compelling bull argument, refute the bear's concerns, and engage in a dynamic debate that demonstrates the strengths of the bull position.
""" + get_language_instruction()

        # LLM을 한 번 호출해 강세론자의 주장 발언을 생성합니다.
        response = llm.invoke(prompt)

        # 발언 앞에 화자 라벨을 붙입니다. (토론 기록에서 누구 발언인지 구분용)
        argument = f"Bull Analyst: {response.content}"

        # 토론 상태를 새로 만들어 돌려줍니다. history와 bull_history에는 이번
        # 발언을 덧붙이고, current_response를 내 발언으로 바꿔 다음 차례의
        # 약세론자(Bear)가 반박할 대상으로 삼게 하며, count(발언 횟수)를 1 올려
        # 토론 종료 조건 판단에 쓰이게 합니다.
        new_investment_debate_state = {
            "history": history + "\n" + argument,
            "bull_history": bull_history + "\n" + argument,
            "bear_history": investment_debate_state.get("bear_history", ""),
            "current_response": argument,
            "count": investment_debate_state["count"] + 1,
        }

        # LangGraph 규칙: 갱신하려는 상태 키만 담은 dict를 반환하면
        # 프레임워크가 전체 상태에 병합해 줍니다.
        return {"investment_debate_state": new_investment_debate_state}

    return bull_node
