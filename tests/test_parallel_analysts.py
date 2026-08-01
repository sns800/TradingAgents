"""[모듈 개요] 분석가 병렬화(설계분석 중기 로드맵 #6) 테스트.

분석가 4종이 단일 messages 채널을 공유해 사실상 직렬로 실행되던 구조
(설계분석-보고서 2.2절: 직렬 4배 지연 + Msg Clear 우회책 + 원본 도구 데이터
파기의 공통 원인)를, 분석가별 전용 메시지 채널 + START fan-out + defer 합류
배리어로 바꾼 재설계를 검증한다:

  1. 그래프 모양 — START에서 선택된 애널리스트 전원으로 fan-out하고,
     합류 배리어(Analyst Join)를 거쳐 토론 선발언자로 이어지는지
  2. 동작 동등성 — 모킹 LLM으로 전체 그래프를 실행했을 때 4종 보고서와
     최종 결정이 예전과 동일하게 생성되는지 (합류 노드는 정확히 1회 실행)
  3. 채널 격리 — 한 애널리스트의 도구 호출/결과(ToolMessage)가 다른
     애널리스트의 채널이나 공유 messages 채널에 섞이지 않는지
  4. 게이트/스냅샷 보존 — market_data_ok(중기 #4)와 verified_snapshot
     (중기 #5)이 새 구조(전용 채널 기준)에서도 그대로 동작하는지
  5. 부분 선택 — 애널리스트 1명 선택과 crypto 모드(fundamentals 제외)
  6. 체크포인트 시그니처 — 토폴로지 버전이 시그니처에 반영되는지

모킹 전략: 가짜 LLM은 with_structured_output을 지원하지 않아(NotImplementedError)
모든 구조화 에이전트가 자유 텍스트 폴백을 타고, bind_tools는 "도구 결과가
없으면 도구 호출, 있으면 최종 보고서"를 반환하는 결정론적 체인을 만든다.
가짜 ToolNode는 실제 langgraph ToolNode에 시험용 도구를 등록해 실제 도구
실행 경로(채널 쓰기 포함)를 그대로 사용한다.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import START
from langgraph.prebuilt import ToolNode

from tradingagents.agents.utils.agent_states import ANALYST_MESSAGE_CHANNELS
from tradingagents.agents.utils.market_data_validation_tools import (
    VERIFIED_SNAPSHOT_HEADER_PREFIX,
)
from tradingagents.graph.analyst_execution import ANALYST_JOIN_NODE
from tradingagents.graph.conditional_logic import ConditionalLogic
from tradingagents.graph.propagation import Propagator
from tradingagents.graph.setup import GraphSetup

ALL_ANALYSTS = ("market", "social", "news", "fundamentals")

SNAPSHOT_BODY = (
    f"{VERIFIED_SNAPSHOT_HEADER_PREFIX} for NVDA\n\n"
    "| Field | Value |\n|---|---:|\n| Close | 123.45 |\n"
)

# 벤더 라우터가 데이터 부재 시 만드는 것과 같은 형태의 NO_DATA 센티널.
NO_DATA_SENTINEL = (
    "NO_DATA_AVAILABLE: no rows. No usable OHLCV data is available for this "
    "symbol. Do not estimate or fabricate values — report that data is "
    "unavailable."
)

# 채널 격리 검증용 — 애널리스트별로 고유한 도구 출력 마커.
TOOL_MARKERS = {
    "market": "MARKET-TOOL-DATA",
    "social": "SOCIAL-TOOL-DATA",
    "news": "NEWS-TOOL-DATA",
    "fundamentals": "FUND-TOOL-DATA",
}


def _fake_tool(name: str, output: str):
    """이름이 실제 도구와 같고 인자가 없는 시험용 도구를 만든다.

    가짜 LLM이 애널리스트에 바인딩된 실제 도구 목록의 첫 번째 이름으로
    도구 호출을 만들기 때문에, ToolNode에는 같은 이름의 시험용 도구를
    등록해야 호출이 실행된다.
    """

    @tool(name)
    def _t() -> str:
        """Deterministic fake data tool for tests."""
        return output

    return _t


class FakeLLM:
    """도구 루프 1회 + 최종 보고서를 결정론적으로 재현하는 가짜 LLM.

    - with_structured_output: 미지원(NotImplementedError) — 모든 구조화
      에이전트(감성/RM/트레이더/PM)가 자유 텍스트 폴백 경로를 탄다.
    - bind_tools: 프롬프트에 ToolMessage가 없으면(첫 방문) 도구 호출을,
      있으면(도구 실행 후 재방문) 최종 보고서를 반환한다.
    - invoke: 자유 텍스트 발언/판정 — PM 폴백의 등급 파싱이 가능하도록
      영어 등급 줄을 포함한다.
    """

    def with_structured_output(self, schema):
        raise NotImplementedError("test LLM: free-text fallback only")

    def bind_tools(self, tools):
        tool_names = [t.name for t in tools]

        def bound(prompt_value):
            messages = prompt_value.to_messages()
            if any(isinstance(m, ToolMessage) for m in messages):
                return AIMessage(content=f"final report via {tool_names[0]}")
            calls = [
                {"name": tool_names[0], "args": {}, "id": f"call-{tool_names[0]}"}
            ]
            # 시장 애널리스트는 프롬프트 지시대로 검증 스냅샷 도구도 호출한다.
            if "get_verified_market_snapshot" in tool_names:
                calls.append(
                    {
                        "name": "get_verified_market_snapshot",
                        "args": {},
                        "id": "call-snap",
                    }
                )
            return AIMessage(content="", tool_calls=calls)

        return bound

    def invoke(self, prompt):
        return AIMessage(content="Rating: Buy\nMocked free-text argument.")


def _fake_tool_nodes(market_output: str = SNAPSHOT_BODY) -> dict[str, ToolNode]:
    """실제 ToolNode에 시험용 도구를 등록한 애널리스트별 도구 노드를 만든다.

    실제 배선(trading_graph._create_tool_nodes)과 동일하게 messages_key로
    전용 채널에 연결한다 — 채널 격리는 이 배선의 산물이므로 테스트도
    같은 방식을 써야 실제 경로를 검증한다.
    """
    return {
        "market": ToolNode(
            [
                _fake_tool("get_stock_data", TOOL_MARKERS["market"]),
                _fake_tool("get_verified_market_snapshot", market_output),
            ],
            messages_key=ANALYST_MESSAGE_CHANNELS["market"],
        ),
        "social": ToolNode(
            # 감성 애널리스트는 도구를 호출하지 않지만, 실제 배선과 동일하게
            # 노드는 존재한다.
            [_fake_tool("get_news", TOOL_MARKERS["social"])],
            messages_key=ANALYST_MESSAGE_CHANNELS["social"],
        ),
        "news": ToolNode(
            [_fake_tool("get_news", TOOL_MARKERS["news"])],
            messages_key=ANALYST_MESSAGE_CHANNELS["news"],
        ),
        "fundamentals": ToolNode(
            [_fake_tool("get_fundamentals", TOOL_MARKERS["fundamentals"])],
            messages_key=ANALYST_MESSAGE_CHANNELS["fundamentals"],
        ),
    }


@pytest.fixture()
def _stub_sentiment_prefetch(monkeypatch):
    """감성 애널리스트의 프리페치(뉴스/StockTwits/Reddit)를 네트워크 없이 스텁."""
    import tradingagents.agents.analysts.sentiment_analyst as sentiment

    monkeypatch.setattr(
        sentiment, "fetch_stocktwits_messages", lambda *a, **k: "stub stocktwits"
    )
    monkeypatch.setattr(sentiment, "fetch_reddit_posts", lambda *a, **k: "stub reddit")
    monkeypatch.setattr(
        sentiment.get_news, "func", lambda *a, **k: "stub news", raising=False
    )


def _run_graph(
    selected_analysts,
    market_output: str = SNAPSHOT_BODY,
    asset_type: str = "stock",
):
    """가짜 LLM/도구로 전체 그래프를 컴파일·실행하고 최종 상태를 반환한다."""
    setup = GraphSetup(
        quick_thinking_llm=FakeLLM(),
        deep_thinking_llm=FakeLLM(),
        tool_nodes=_fake_tool_nodes(market_output),
        conditional_logic=ConditionalLogic(),
    )
    graph = setup.setup_graph(selected_analysts=selected_analysts).compile()
    state = Propagator().create_initial_state(
        "NVDA", "2026-01-01", asset_type=asset_type
    )
    return graph.invoke(state)


# ---------------------------------------------------------------------------
# 1. 그래프 모양: START fan-out + 합류 배리어
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGraphShape:
    def test_start_fans_out_to_all_selected_analysts(self):
        """START에서 선택된 애널리스트 전원으로 동시에 진입하는지 검증하는 테스트."""
        setup = GraphSetup(FakeLLM(), FakeLLM(), _fake_tool_nodes(), ConditionalLogic())
        workflow = setup.setup_graph(selected_analysts=ALL_ANALYSTS)

        for agent_node in (
            "Market Analyst", "Sentiment Analyst", "News Analyst",
            "Fundamentals Analyst",
        ):
            assert (START, agent_node) in workflow.edges, (
                f"{agent_node} must start directly from START (parallel fan-out)"
            )
        # 합류 배리어에서 토론 선발언자(기본 Bull)로 이어진다.
        assert (ANALYST_JOIN_NODE, "Bull Researcher") in workflow.edges

    def test_msg_clear_nodes_are_gone(self):
        """Msg Clear 노드가 그래프에서 완전히 제거됐는지 검증하는 테스트 (중기 #6)."""
        setup = GraphSetup(FakeLLM(), FakeLLM(), _fake_tool_nodes(), ConditionalLogic())
        workflow = setup.setup_graph(selected_analysts=ALL_ANALYSTS)
        clear_nodes = [n for n in workflow.nodes if n.startswith("Msg Clear")]
        assert clear_nodes == []

    def test_join_node_is_deferred(self):
        """합류 노드가 defer(모든 분기 완료까지 대기) 플래그로 등록됐는지 검증하는 테스트.

        defer가 없으면 첫 애널리스트가 끝나는 순간 토론이 시작되어
        나머지 보고서 없이 판정이 진행된다 — 배리어의 핵심 속성이다.
        """
        setup = GraphSetup(FakeLLM(), FakeLLM(), _fake_tool_nodes(), ConditionalLogic())
        workflow = setup.setup_graph(selected_analysts=ALL_ANALYSTS)
        assert workflow.nodes[ANALYST_JOIN_NODE].defer is True


# ---------------------------------------------------------------------------
# 2. 동작 동등성: 전체 그래프 실행으로 4종 보고서 + 최종 결정 생성
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFullGraphEquivalence:
    def test_all_four_reports_and_decision_are_produced(self, _stub_sentiment_prefetch):
        """병렬 구조에서도 4종 보고서와 최종 결정이 전부 생성되는지 검증하는 테스트."""
        final = _run_graph(ALL_ANALYSTS)

        for report_key in (
            "market_report", "sentiment_report", "news_report",
            "fundamentals_report",
        ):
            assert final.get(report_key), f"{report_key} must be produced"
        assert final.get("investment_plan")
        assert final.get("trader_investment_plan")
        assert final.get("final_trade_decision")

    def test_shared_messages_channel_untouched_by_analysts(
        self, _stub_sentiment_prefetch
    ):
        """애널리스트 단계가 공유 messages 채널에 아무것도 쓰지 않는지 검증하는 테스트.

        공유 채널에는 초기 시드(Human)와 트레이더의 결과 기록(AI)만 남아야
        한다 — 하위 호환용으로 유지하되 애널리스트는 쓰지 않는다는 계약.
        """
        final = _run_graph(ALL_ANALYSTS)
        assert [type(m).__name__ for m in final["messages"]] == [
            "HumanMessage", "AIMessage",
        ]
        assert not any(isinstance(m, ToolMessage) for m in final["messages"])

    def test_single_analyst_selection_still_works(self, _stub_sentiment_prefetch):
        """애널리스트 1명만 선택해도(fan-out 폭 1) 그래프가 끝까지 도는지 검증하는 테스트."""
        final = _run_graph(("market",))
        assert final.get("market_report")
        assert final.get("final_trade_decision")
        # 선택되지 않은 애널리스트의 보고서는 초기값(빈 문자열) 그대로다.
        assert final.get("news_report") == ""

    def test_crypto_mode_without_fundamentals(self, _stub_sentiment_prefetch):
        """crypto 모드(fundamentals 제외 3종 선택)에서도 동작하는지 검증하는 테스트."""
        final = _run_graph(
            ("market", "social", "news"), asset_type="crypto"
        )
        assert final.get("market_report")
        assert final.get("sentiment_report")
        assert final.get("news_report")
        assert final.get("fundamentals_report") == ""
        assert final.get("final_trade_decision")


# ---------------------------------------------------------------------------
# 3. 채널 격리
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestChannelIsolation:
    def test_tool_messages_stay_in_their_own_channel(self, _stub_sentiment_prefetch):
        """한 애널리스트의 도구 결과가 다른 채널에 섞이지 않는지 검증하는 테스트."""
        final = _run_graph(ALL_ANALYSTS)

        def channel_text(analyst_key: str) -> str:
            channel = ANALYST_MESSAGE_CHANNELS[analyst_key]
            return "\n".join(
                str(getattr(m, "content", "")) for m in final[channel]
            )

        # 자기 채널에는 자기 도구 마커가 있어야 한다 (social은 도구 미사용).
        for analyst_key in ("market", "news", "fundamentals"):
            assert TOOL_MARKERS[analyst_key] in channel_text(analyst_key)

        # 다른 채널에는 절대 없어야 한다.
        for analyst_key in ALL_ANALYSTS:
            for other_key in ALL_ANALYSTS:
                if other_key == analyst_key:
                    continue
                assert TOOL_MARKERS[other_key] not in channel_text(analyst_key), (
                    f"{other_key} tool output leaked into "
                    f"{ANALYST_MESSAGE_CHANNELS[analyst_key]}"
                )

        # 공유 messages 채널에도 도구 마커가 없어야 한다.
        shared = "\n".join(str(getattr(m, "content", "")) for m in final["messages"])
        for marker in TOOL_MARKERS.values():
            assert marker not in shared

    def test_tool_loop_routes_by_own_channel(self):
        """라우터가 자기 채널만 보고 도구 루프/합류를 결정하는지 검증하는 테스트.

        다른 애널리스트의 채널에 도구 호출이 남아 있어도(병렬 실행 중 흔한
        상황) 자기 채널이 완료 상태면 합류로 가야 한다.
        """
        logic = ConditionalLogic()
        pending_tool_call = AIMessage(
            content="", tool_calls=[{"name": "get_news", "args": {}, "id": "c1"}]
        )
        state = {
            # 시장 채널은 보고서 완성(도구 호출 없음), 뉴스 채널은 도구 호출 대기.
            "market_messages": [AIMessage(content="market report done")],
            "news_messages": [pending_tool_call],
        }
        assert logic.should_continue_market(state) == ANALYST_JOIN_NODE
        assert logic.should_continue_news(state) == "tools_news"


# ---------------------------------------------------------------------------
# 4. market_data_ok 게이트(중기 #4) + verified_snapshot 보존(중기 #5)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGateAndSnapshotPreservation:
    def test_verified_snapshot_survives_to_final_state(self, _stub_sentiment_prefetch):
        """검증 스냅샷이 병렬 구조에서도 최종 상태까지 보존되는지 검증하는 테스트."""
        final = _run_graph(ALL_ANALYSTS)
        assert final["verified_snapshot"] == SNAPSHOT_BODY
        assert final["market_data_ok"] is True

    def test_no_data_sentinel_forces_hold_end_to_end(self, _stub_sentiment_prefetch):
        """시장 도구가 NO_DATA를 반환하면 병렬 구조에서도 강제 Hold로 끝나는지 검증.

        시장 애널리스트의 게이트(_market_data_ok)가 전용 채널의 도구 결과를
        읽고 플래그를 내리며, PM이 LLM 판정 없이 Hold를 강제한다 — 중기 #4
        경로가 채널 분리 후에도 끊기지 않았음을 전체 그래프로 확인한다.
        """
        from tradingagents.agents.managers.portfolio_manager import FORCED_HOLD_REASON

        final = _run_graph(ALL_ANALYSTS, market_output=NO_DATA_SENTINEL)
        assert final["market_data_ok"] is False
        assert final["verified_snapshot"] == ""
        assert FORCED_HOLD_REASON in final["final_trade_decision"]


# ---------------------------------------------------------------------------
# 5. 체크포인트 시그니처의 토폴로지 버전
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_run_signature_includes_topology_version():
    """_run_signature에 병렬 토폴로지 버전이 포함되는지 검증하는 테스트.

    같은 selected_analysts라도 병렬화 이전(직렬 체인 + Msg Clear)과 이후의
    그래프 모양·상태 스키마가 다르므로, 구버전 체크포인트에서 조용히
    재개하지 않도록 정적 토큰이 스레드 ID를 갈라야 한다 (중기 #6).
    """
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    g = object.__new__(TradingAgentsGraph)
    g.selected_analysts = ("market", "news")
    g.config = {"max_debate_rounds": 1, "max_risk_discuss_rounds": 1}
    signature = g._run_signature("stock")
    assert "topology=parallel-analysts-v1" in signature
