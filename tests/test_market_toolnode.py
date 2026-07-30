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
