# =============================================================================
# [모듈 개요 — 초보자용]
# 이 파일은 예측 시장(prediction markets, 예: Polymarket)에서 미래 이벤트의
# 시장 내재 확률(market-implied probability)을 가져오는 LangChain 툴(tool)을 정의합니다.
# 예: 연준(Fed) 금리 결정, 경기 침체, 선거 결과 등의 발생 확률을 실제 베팅 시장 가격으로
# 추정한 값입니다. TradingAgents 시스템에서 거시/뉴스 분석 에이전트가 미래 이벤트
# 리스크를 평가할 때 사용하며, route_to_vendor()가 설정된 prediction_markets
# 공급자(vendor)로 라우팅합니다.
# =============================================================================

from typing import Annotated

from langchain_core.tools import tool

from tradingagents.dataflows.interface import route_to_vendor


# [한국어 설명] 예측 시장(Polymarket)에서 주제(topic)에 맞는 미래 이벤트의 실시간
# 내재 확률을 조회하는 툴. 주제와 일치하는 거래량 상위 오픈 마켓들을 내재 확률,
# 거래량, 결정(resolution) 날짜, 최근 변동과 함께 반환한다.
# 아래 docstring은 LLM에게 툴 설명으로 전달되므로 영어 원문을 유지한다.
@tool
def get_prediction_markets(
    topic: Annotated[
        str,
        "Event topic/keyword, e.g. 'Fed rate cut', 'recession 2026', "
        "'US election', or a sector/company event.",
    ],
    limit: Annotated[int | None, "Max markets to return; omit for a default of 6"] = None,
) -> str:
    """
    Retrieve live, market-implied probabilities for forward-looking events from
    prediction markets (Polymarket): Fed decisions, recession, elections,
    geopolitics, crypto. Returns the most-traded open markets matching the
    topic, each with its implied probability, traded volume, resolution date,
    and recent move. Uses the configured prediction_markets vendor.

    Args:
        topic (str): Event keyword(s) to search
        limit (int): Max markets to return; omit for a default of 6

    Returns:
        str: A formatted markdown report of matching prediction markets
    """
    return route_to_vendor("get_prediction_markets", topic, limit)
