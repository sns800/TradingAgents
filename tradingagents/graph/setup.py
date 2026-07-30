# TradingAgents/graph/setup.py
#
# [모듈 개요 - 초보자용]
# 이 파일은 LangGraph의 StateGraph를 이용해 에이전트 워크플로 그래프
# (graph, 에이전트들의 실행 순서를 정의한 흐름도)를 실제로 "조립"하는 곳입니다.
# 애널리스트들 -> 강세/약세 연구원 토론 -> 리서치 매니저 -> 트레이더 ->
# 리스크 토론(3인) -> 포트폴리오 매니저 순서로 노드(node, 실행 단위)와
# 엣지(edge, 노드 사이의 이동 경로)를 등록합니다.
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
    create_msg_delete,
    create_neutral_debator,
    create_news_analyst,
    create_portfolio_manager,
    create_research_manager,
    create_sentiment_analyst,
    create_trader,
)
from tradingagents.agents.utils.agent_states import AgentState

from .analyst_execution import build_analyst_execution_plan
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
        # 애널리스트 한 명당 노드 3개가 등록됩니다:
        #   agent_node(분석 담당 LLM) / clear_node(대화 메시지 정리) /
        #   tool_node(데이터 조회 도구 실행)
        for spec in plan.specs:
            workflow.add_node(spec.agent_node, analyst_factories[spec.key]())
            workflow.add_node(spec.clear_node, create_msg_delete())
            workflow.add_node(spec.tool_node, self.tool_nodes[spec.key])

        # 나머지 노드 추가
        workflow.add_node("Bull Researcher", bull_researcher_node)
        workflow.add_node("Bear Researcher", bear_researcher_node)
        workflow.add_node("Research Manager", research_manager_node)
        workflow.add_node("Trader", trader_node)
        workflow.add_node("Aggressive Analyst", aggressive_analyst)
        workflow.add_node("Neutral Analyst", neutral_analyst)
        workflow.add_node("Conservative Analyst", conservative_analyst)
        workflow.add_node("Portfolio Manager", portfolio_manager_node)

        # 엣지(edge) 정의
        # START(그래프 시작점)에서 첫 번째 애널리스트로 진입
        workflow.add_edge(START, plan.specs[0].agent_node)

        # 애널리스트들을 순서대로 연결.
        # 각 애널리스트는 "도구 호출이 남아 있으면 tool_node로 갔다가 다시
        # 자신에게 돌아오고(루프), 분석이 끝나면 clear_node를 거쳐 다음
        # 애널리스트로 넘어가는" 구조입니다.
        for i, spec in enumerate(plan.specs):
            current_analyst = spec.agent_node
            current_tools = spec.tool_node
            current_clear = spec.clear_node

            # 현재 애널리스트에 대한 조건부 엣지 추가
            # (conditional_logic의 should_continue_<key> 메서드가 라우터 역할)
            workflow.add_conditional_edges(
                current_analyst,
                getattr(self.conditional_logic, f"should_continue_{spec.key}"),
                [current_tools, current_clear],
            )
            workflow.add_edge(current_tools, current_analyst)

            # 다음 애널리스트로 연결하고, 마지막 애널리스트라면 Bull Researcher로 연결
            if i < len(plan.specs) - 1:
                workflow.add_edge(current_clear, plan.specs[i + 1].agent_node)
            else:
                workflow.add_edge(current_clear, "Bull Researcher")

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
