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

    def __init__(
        self,
        max_debate_rounds=1,
        max_risk_discuss_rounds=1,
        debate_first_speaker="bull",
    ):
        """설정 파라미터로 초기화한다.

        Args:
            max_debate_rounds: 강세/약세 리서처 토론의 라운드 수(N).
                실제 발언 수는 2N+1 — 아래 should_continue_debate 참고.
            max_risk_discuss_rounds: 리스크 3자 토론의 라운드 수(N).
                실제 발언 수는 3N+1 — 아래 should_continue_risk_analysis 참고.
            debate_first_speaker: 리서처 토론의 선발언자 ("bull" 또는 "bear").
                기본값 "bull"은 기존 동작(Bull이 개시 발언)을 그대로 보존한다.
                setup.py의 진입 엣지와 이 클래스의 드리프트 폴백 라우팅이
                모두 이 값을 따른다. 리스크 토론의 발언 순서는 이번 범위에서
                의도적으로 제외했다 — 3자 순환이라 순서 조합이 6가지로 늘어
                복잡도 대비 효과가 낮다 (설계분석 중기 로드맵 #3).
        """
        self.max_debate_rounds = max_debate_rounds
        self.max_risk_discuss_rounds = max_risk_discuss_rounds
        normalized_first_speaker = str(debate_first_speaker).strip().lower()
        if normalized_first_speaker not in ("bull", "bear"):
            # 잘못된 설정은 조용히 기본값으로 되돌리지 않고 시작 시점에 크게
            # 실패한다 (default_config._coerce와 동일한 철학).
            raise ValueError(
                "debate_first_speaker must be 'bull' or 'bear', "
                f"got {debate_first_speaker!r}"
            )
        self.debate_first_speaker = normalized_first_speaker

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

        count는 발언 횟수입니다. 종료 조건은 2N+1 발언(N = max_debate_rounds):
        예전의 2N 종료는 항상 후발언자(기본: Bear)의 발언 직후에 끝나서
        선발언자가 마지막 비판에 응답할 기회가 0회였고, 최후 발언 편향이
        라운드 수와 무관하게 고정됐습니다 (설계분석 2.3 / 중기 로드맵 #3).
        +1 발언을 추가해 선발언자가 마지막 비판에 한 번 재반박한 뒤 심판
        (Research Manager)에게 넘어갑니다. 번갈아 말하므로 홀수 번째 발언은
        항상 선발언자 차례 — 2N+1에서 끝나면 마지막 발언자는 선발언자입니다.

        (향후 과제) 양측 논지가 수렴하면 조기 종료하는 설계는 수렴 감지용
        LLM 호출이 추가로 필요해 비용 역효과가 있으므로 의도적으로 구현하지
        않았습니다 — 발언 수 기반의 결정론적 종료만 사용합니다.
        """

        if (
            state["investment_debate_state"]["count"]
            >= 2 * self.max_debate_rounds + 1
        ):  # 에이전트 2명 x N라운드 + 선발언자의 마지막 재반박 1회
            return "Research Manager"
        current_response = state["investment_debate_state"]["current_response"]
        if current_response.startswith("Bull"):
            return "Bear Researcher"
        if current_response.startswith("Bear"):
            return "Bull Researcher"
        # 드리프트 폴백: 발언자 라벨이 비었거나 알 수 없는 값이면(#1088)
        # 설정된 선발언자에게 차례를 준다. 기본값 "bull"에서는 기존 동작
        # (알 수 없는 라벨 -> Bull Researcher)과 동일하다.
        if self.debate_first_speaker == "bear":
            return "Bear Researcher"
        return "Bull Researcher"

    def should_continue_risk_analysis(self, state: AgentState) -> str:
        """리스크(risk) 분석 토론을 계속할지 판단한다.

        리스크 토론에는 에이전트 3명(공격적/보수적/중립)이 참여합니다.
        종료 조건은 3N+1 발언(N = max_risk_discuss_rounds): 예전의 3N 종료는
        항상 Neutral 발언 직후에 끝나서 Aggressive가 Conservative/Neutral의
        비판에 응답할 기회가 0회였습니다 (설계분석 2.3 / 중기 로드맵 #3).
        +1 발언을 추가해 선발언자(Aggressive)가 마지막 비판에 한 번 재반박한
        뒤 Portfolio Manager에게 넘어갑니다. 발언 순서 설정화는 리서처 토론과
        달리 이번 범위에서 제외 — 3자 순환이라 순서 조합(6가지) 복잡도 대비
        효과가 낮습니다. 수렴 기반 조기 종료도 should_continue_debate와 같은
        이유(추가 LLM 호출 비용)로 향후 과제로 남깁니다.
        """
        if (
            state["risk_debate_state"]["count"]
            >= 3 * self.max_risk_discuss_rounds + 1
        ):  # 에이전트 3명 x N라운드 + 선발언자(Aggressive)의 마지막 재반박 1회
            return "Portfolio Manager"
        if state["risk_debate_state"]["latest_speaker"].startswith("Aggressive"):
            return "Conservative Analyst"
        if state["risk_debate_state"]["latest_speaker"].startswith("Conservative"):
            return "Neutral Analyst"
        return "Aggressive Analyst"
