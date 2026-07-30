# TradingAgents/graph/conditional_logic.py
#
# [모듈 개요 - 초보자용]
# 이 파일은 그래프(graph, 에이전트들의 실행 순서를 정의한 흐름도)의
# 조건부 라우팅(conditional routing, 상태에 따라 다음에 실행할 노드를 고르는 것)
# 규칙을 모아둔 곳입니다. 예를 들어 "애널리스트가 도구를 더 호출하려 하는가?",
# "강세/약세 토론을 계속할 것인가, 매니저에게 넘길 것인가?" 같은 판단을 합니다.
# setup.py가 add_conditional_edges()로 그래프를 조립할 때 이 클래스의 메서드들을
# 라우터(router) 함수로 등록합니다. 각 메서드가 반환하는 문자열은 그래프에
# 등록된 노드 이름과 정확히 일치해야 하므로 절대 번역하면 안 됩니다.

from tradingagents.agents.utils.agent_states import AgentState


class ConditionalLogic:
    """그래프 흐름을 결정하는 조건부 로직을 담당하는 클래스."""

    def __init__(self, max_debate_rounds=1, max_risk_discuss_rounds=1):
        """설정 파라미터로 초기화한다."""
        self.max_debate_rounds = max_debate_rounds
        self.max_risk_discuss_rounds = max_risk_discuss_rounds

    def should_continue_market(self, state: AgentState):
        """시장(market) 분석을 계속할지 판단한다.

        마지막 메시지에 도구 호출(tool call)이 남아 있으면 도구 노드로 보내고,
        없으면(분석이 끝났으면) 메시지 정리 노드로 보내 다음 단계로 넘어갑니다.
        """
        messages = state["messages"]
        last_message = messages[-1]
        if last_message.tool_calls:
            return "tools_market"
        return "Msg Clear Market"

    def should_continue_social(self, state: AgentState):
        """감성(sentiment) 애널리스트의 도구 호출 라운드를 계속할지 판단한다.

        메서드 이름은 저장된 설정과의 하위 호환을 위해 기존
        ``AnalystType.SOCIAL = "social"`` 값에 맞춰 ``social`` 접미사를
        유지합니다. 반환하는 ``clear_node`` 라벨은 v0.2.5에서 바뀐 이름을
        사용해, 실행 계획(execution plan)이 등록한 노드 이름과 일치합니다.
        """
        messages = state["messages"]
        last_message = messages[-1]
        if last_message.tool_calls:
            return "tools_social"
        return "Msg Clear Sentiment"

    def should_continue_news(self, state: AgentState):
        """뉴스(news) 분석을 계속할지 판단한다."""
        messages = state["messages"]
        last_message = messages[-1]
        if last_message.tool_calls:
            return "tools_news"
        return "Msg Clear News"

    def should_continue_fundamentals(self, state: AgentState):
        """재무(fundamentals) 분석을 계속할지 판단한다."""
        messages = state["messages"]
        last_message = messages[-1]
        if last_message.tool_calls:
            return "tools_fundamentals"
        return "Msg Clear Fundamentals"

    def should_continue_debate(self, state: AgentState) -> str:
        """강세/약세(bull/bear) 투자 토론을 계속할지 판단한다.

        count는 발언 횟수입니다. 에이전트 2명이 번갈아 말하므로
        라운드 수 x 2에 도달하면 토론을 끝내고 Research Manager에게 넘깁니다.
        아직이라면 직전 발언자의 반대편(Bull <-> Bear)에게 차례를 줍니다.
        """

        if (
            state["investment_debate_state"]["count"] >= 2 * self.max_debate_rounds
        ):  # 에이전트 2명이 주고받는 라운드 기준
            return "Research Manager"
        if state["investment_debate_state"]["current_response"].startswith("Bull"):
            return "Bear Researcher"
        return "Bull Researcher"

    def should_continue_risk_analysis(self, state: AgentState) -> str:
        """리스크(risk) 분석 토론을 계속할지 판단한다.

        리스크 토론에는 에이전트 3명(공격적/보수적/중립)이 참여하므로
        라운드 수 x 3 발언에 도달하면 Portfolio Manager에게 넘깁니다.
        아직이라면 공격적 -> 보수적 -> 중립 -> 공격적... 순으로 순환시킵니다.
        """
        if (
            state["risk_debate_state"]["count"] >= 3 * self.max_risk_discuss_rounds
        ):  # 에이전트 3명이 주고받는 라운드 기준
            return "Portfolio Manager"
        if state["risk_debate_state"]["latest_speaker"].startswith("Aggressive"):
            return "Conservative Analyst"
        if state["risk_debate_state"]["latest_speaker"].startswith("Conservative"):
            return "Neutral Analyst"
        return "Aggressive Analyst"
