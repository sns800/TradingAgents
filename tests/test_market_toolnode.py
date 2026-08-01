# 이 파일은 시장 분석가(market analyst)가 호출하는 검증 스냅샷 도구가
# 실행 측 ToolNode에도 등록되어 있는지 검증하는 테스트입니다.
"""시장 분석가는 get_verified_market_snapshot을 호출하도록 바인딩되어 있고
프롬프트로도 지시받습니다. 실행기 ToolNode가 이 도구를 등록하지 않으면 호출이
실패하고, 모델은 도구가 "사용 불가"라고 보고하며 검증을 건너뜁니다.

그 배선 누락(도구가 LLM에는 바인딩됐지만 시장 ToolNode에는 빠진 상태)에 대한
회귀(regression) 방지 테스트입니다.
"""
import pytest

from tradingagents.graph.trading_graph import TradingAgentsGraph


@pytest.mark.unit
def test_market_toolnode_can_execute_verified_snapshot():
    """시장 ToolNode에 검증 스냅샷 도구와 핵심 도구들이 등록되어 있는지 검증하는 테스트."""
    # _create_tool_nodes는 self를 사용하지 않음 -> 언바운드로 호출 (LLM 생성을 피함).
    nodes = TradingAgentsGraph._create_tool_nodes(None)
    market_tools = set(nodes["market"].tools_by_name)
    assert "get_verified_market_snapshot" in market_tools, (
        "get_verified_market_snapshot is bound to the market analyst but not "
        "registered in the market ToolNode, so the model's call fails."
    )
    # 다른 핵심 시장 도구들도 그대로 남아 있어야 함
    assert {"get_stock_data", "get_indicators"} <= market_tools


@pytest.mark.unit
def test_tool_nodes_are_wired_to_their_own_message_channels():
    """각 ToolNode가 담당 애널리스트의 전용 메시지 채널에 연결됐는지 검증하는 테스트.

    분석가 병렬화(설계분석 중기 로드맵 #6): ToolNode가 공유 messages 채널을
    쓰면 병렬 실행 중 다른 애널리스트의 도구 호출/결과와 섞이므로,
    messages_key가 애널리스트별 채널로 지정되어 있어야 한다.
    """
    from tradingagents.agents.utils.agent_states import ANALYST_MESSAGE_CHANNELS

    nodes = TradingAgentsGraph._create_tool_nodes(None)
    for analyst_key, channel in ANALYST_MESSAGE_CHANNELS.items():
        # langgraph의 ToolNode는 messages_key를 비공개 속성(_messages_key)으로
        # 보관한다 — 공개 접근자가 없어 여기서는 그 속성을 직접 확인한다.
        assert nodes[analyst_key]._messages_key == channel, (
            f"ToolNode for {analyst_key!r} must read/write its dedicated "
            f"channel {channel!r}, not the shared messages channel"
        )
