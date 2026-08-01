# TradingAgents/graph/setup.py
#
# [모듈 개요 - 초보자용]
# 이 파일은 LangGraph의 StateGraph를 이용해 에이전트 워크플로 그래프
# (graph, 에이전트들의 실행 순서를 정의한 흐름도)를 실제로 "조립"하는 곳입니다.
# 애널리스트들(병렬) -> 합류(join) -> 강세/약세 연구원 토론 -> 리서치 매니저 ->
# 트레이더 -> 리스크 토론(3인) -> 포트폴리오 매니저 순서로 노드(node, 실행
# 단위)와 엣지(edge, 노드 사이의 이동 경로)를 등록합니다.
# trading_graph.py가 초기화될 때 이 GraphSetup.setup_graph()를 호출해
# 컴파일 전의 워크플로 객체를 받아 갑니다.

from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from tradingagents.agents import (
    create_aggressive_debator,
    create_bear_researcher,
    create_bull_researcher,
    create_conservative_debator,
    create_fundamentals_analyst,
    create_market_analyst,
    create_neutral_debator,
    create_news_analyst,
    create_portfolio_manager,
    create_research_manager,
    create_sentiment_analyst,
    create_trader,
)
from tradingagents.agents.utils.agent_states import AgentState

from .analyst_execution import ANALYST_JOIN_NODE, build_analyst_execution_plan
from .conditional_logic import ConditionalLogic

# 공유 조건부 라우터(conditional router)가 반환할 수 있는 모든 목적지 목록.
# 라우터가 쓰이는 모든 엣지에 이 전체 매핑을 등록해 두면, 발언자 라벨이
# 프롬프트 변경/국제화(i18n)/리팩터링으로 어긋나 예상 밖의 값이 반환되더라도
# path_map에 없는 항목을 만나 LangGraph가 실행 도중 죽는 일이 없습니다(#1088).
DEBATE_PATH_MAP = {
    "Bull Researcher": "Bull Researcher",
    "Bear Researcher": "Bear Researcher",
    "Research Manager": "Research Manager",
}
RISK_ANALYSIS_PATH_MAP = {
    "Aggressive Analyst": "Aggressive Analyst",
    "Conservative Analyst": "Conservative Analyst",
    "Neutral Analyst": "Neutral Analyst",
    "Portfolio Manager": "Portfolio Manager",
}


def analyst_join_node(state: AgentState) -> dict:
    """애널리스트 병렬 분기의 합류(join) 배리어 노드 — 상태를 바꾸지 않는다.

    [중기 로드맵 #6] setup_graph()가 이 노드를 ``defer=True``로 등록하므로,
    LangGraph는 선택된 애널리스트 분기 전원(각자의 도구 호출 루프 포함)이
    끝날 때까지 실행을 미뤘다가 정확히 한 번만 이 노드를 실행하고 토론
    단계로 넘어갑니다. 예전 Msg Clear 노드가 하던 "다음 애널리스트를 위한
    대화 비우기"는 채널 분리로 불필요해졌고, 토론 단계는 애널리스트 채널을
    아예 읽지 않으므로 토론 진입 전 정리도 필요 없습니다 — 오히려 도구
    원본 데이터가 상태에 보존되는 것이 설계 목표입니다(원본 파기 문제 해소,
    설계분석-보고서 2.2절). 따라서 이 노드는 의도적으로 아무 갱신도 하지
    않는 순수 배리어입니다.
    """
    return {}


class GraphSetup:
    """에이전트 그래프의 구성(setup)과 설정을 담당하는 클래스."""

    def __init__(
        self,
        quick_thinking_llm: Any,
        deep_thinking_llm: Any,
        tool_nodes: dict[str, ToolNode],
        conditional_logic: ConditionalLogic,
    ):
        """필요한 구성 요소들로 초기화한다."""
        self.quick_thinking_llm = quick_thinking_llm
        self.deep_thinking_llm = deep_thinking_llm
        self.tool_nodes = tool_nodes
        self.conditional_logic = conditional_logic

    def setup_graph(
        self, selected_analysts=("market", "social", "news", "fundamentals")
    ):
        """에이전트 워크플로 그래프를 구성하고 컴파일 가능한 형태로 반환한다.

        Args:
            selected_analysts (list): 포함할 애널리스트 종류의 목록. 선택지:
                - "market": 시장 애널리스트
                - "social": 소셜/감성 애널리스트
                - "news": 뉴스 애널리스트
                - "fundamentals": 재무 애널리스트
        """
        plan = build_analyst_execution_plan(selected_analysts)

        # 애널리스트 키 -> 노드 생성 함수 매핑. lambda로 감싸 두었으므로
        # 실제로 선택된 애널리스트의 노드만 생성됩니다.
        analyst_factories = {
            "market": lambda: create_market_analyst(self.quick_thinking_llm),
            "social": lambda: create_sentiment_analyst(self.quick_thinking_llm),
            "news": lambda: create_news_analyst(self.quick_thinking_llm),
            "fundamentals": lambda: create_fundamentals_analyst(self.quick_thinking_llm),
        }

        # 연구원(researcher)과 매니저 노드 생성
        bull_researcher_node = create_bull_researcher(self.quick_thinking_llm)
        bear_researcher_node = create_bear_researcher(self.quick_thinking_llm)
        research_manager_node = create_research_manager(self.deep_thinking_llm)
        trader_node = create_trader(self.quick_thinking_llm)

        # 리스크 분석 노드 생성
        aggressive_analyst = create_aggressive_debator(self.quick_thinking_llm)
        neutral_analyst = create_neutral_debator(self.quick_thinking_llm)
        conservative_analyst = create_conservative_debator(self.quick_thinking_llm)
        portfolio_manager_node = create_portfolio_manager(self.deep_thinking_llm)

        # 워크플로 생성. AgentState가 모든 노드가 공유하는 상태 스키마입니다.
        workflow = StateGraph(AgentState)

        # 애널리스트 노드들을 그래프에 추가.
        # 애널리스트 한 명당 노드 2개가 등록됩니다:
        #   agent_node(분석 담당 LLM) / tool_node(데이터 조회 도구 실행)
        # 예전의 clear_node(Msg Clear)는 중기 로드맵 #6에서 제거 — 애널리스트
        # 별 전용 메시지 채널 덕분에 다음 애널리스트를 위해 대화를 비울
        # 필요가 없어졌습니다.
        for spec in plan.specs:
            workflow.add_node(spec.agent_node, analyst_factories[spec.key]())
            workflow.add_node(spec.tool_node, self.tool_nodes[spec.key])

        # 합류 배리어 노드. defer=True가 핵심입니다: LangGraph는 다른 실행
        # 가능한 태스크가 남아 있는 동안 이 노드를 미루므로, 도구 루프 횟수가
        # 제각각인 병렬 분기들이 전부 끝난 뒤에야 한 번 실행됩니다(map-reduce
        # 합류 패턴).
        workflow.add_node(ANALYST_JOIN_NODE, analyst_join_node, defer=True)

        # 나머지 노드 추가
        workflow.add_node("Bull Researcher", bull_researcher_node)
        workflow.add_node("Bear Researcher", bear_researcher_node)
        workflow.add_node("Research Manager", research_manager_node)
        workflow.add_node("Trader", trader_node)
        workflow.add_node("Aggressive Analyst", aggressive_analyst)
        workflow.add_node("Neutral Analyst", neutral_analyst)
        workflow.add_node("Conservative Analyst", conservative_analyst)
        workflow.add_node("Portfolio Manager", portfolio_manager_node)

        # 리서처 토론의 선발언자 노드. debate_first_speaker 설정(기본 "bull" —
        # 기존 동작 보존)을 따르며, 라우팅 폴백과의 일관성을 위해
        # conditional_logic이 검증·정규화한 값을 단일 소스로 읽는다.
        # 리스크 토론(3자)의 발언 순서 설정화는 이번 범위에서 제외 —
        # 순서 조합이 6가지로 늘어 복잡도 대비 효과가 낮다 (중기 로드맵 #3).
        first_debater_node = (
            "Bear Researcher"
            if getattr(self.conditional_logic, "debate_first_speaker", "bull")
            == "bear"
            else "Bull Researcher"
        )

        # 엣지(edge) 정의 (중기 로드맵 #6 — 분석가 병렬화)
        #
        # START에서 선택된 애널리스트 전원으로 동시에 진입(fan-out)합니다.
        # 각 애널리스트는 자기 전용 메시지 채널 기준으로 "도구 호출이 남아
        # 있으면 tool_node로 갔다가 자신에게 돌아오는(루프)" 구조를 유지하고,
        # 분석이 끝나면 합류 노드(Analyst Join)로 향합니다. 합류 노드는
        # defer=True 배리어라 모든 분기(각자 루프 길이가 달라도)가 끝난 뒤
        # 정확히 한 번 실행되며, 애널리스트가 1명뿐이어도 동일하게 동작합니다.
        for spec in plan.specs:
            workflow.add_edge(START, spec.agent_node)
            # 애널리스트별 조건부 엣지: conditional_logic의
            # should_continue_<key> 메서드가 전용 채널을 보고 라우팅합니다.
            workflow.add_conditional_edges(
                spec.agent_node,
                getattr(self.conditional_logic, f"should_continue_{spec.key}"),
                [spec.tool_node, ANALYST_JOIN_NODE],
            )
            workflow.add_edge(spec.tool_node, spec.agent_node)

        # 합류 후 리서처 토론의 선발언자로 진입합니다.
        workflow.add_edge(ANALYST_JOIN_NODE, first_debater_node)

        # 연구 토론 엣지 두 개는 완전한 DEBATE_PATH_MAP을 공유한다 (#1088).
        for debate_node in ("Bull Researcher", "Bear Researcher"):
            workflow.add_conditional_edges(
                debate_node,
                self.conditional_logic.should_continue_debate,
                DEBATE_PATH_MAP,
            )
        workflow.add_edge("Research Manager", "Trader")
        workflow.add_edge("Trader", "Aggressive Analyst")
        # 리스크 토론 엣지 세 개는 완전한 RISK_ANALYSIS_PATH_MAP을 공유한다 (#1088).
        for risk_node in ("Aggressive Analyst", "Conservative Analyst", "Neutral Analyst"):
            workflow.add_conditional_edges(
                risk_node,
                self.conditional_logic.should_continue_risk_analysis,
                RISK_ANALYSIS_PATH_MAP,
            )

        # 포트폴리오 매니저의 최종 결정으로 그래프 종료(END)
        workflow.add_edge("Portfolio Manager", END)

        return workflow
