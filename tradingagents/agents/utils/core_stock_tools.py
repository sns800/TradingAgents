# =============================================================================
# [모듈 개요 — 초보자용]
# 이 파일은 주가 데이터(시가/고가/저가/종가/거래량, OHLCV)를 가져오는 LangChain 툴(tool)을
# 정의합니다. TradingAgents(LLM 멀티 에이전트 주식 트레이딩 프레임워크)에서
# 시장 분석가(Market Analyst) 등 에이전트가 LLM 툴 호출(tool calling)로 이 함수를 실행해
# 실제 주가 데이터를 조회합니다. 실제 데이터 조회는 route_to_vendor()가 설정된
# 데이터 공급자(vendor)로 요청을 라우팅하여 처리합니다.
# =============================================================================

from typing import Annotated

from langchain_core.tools import tool

from tradingagents.dataflows.interface import route_to_vendor


# [한국어 설명] 지정한 티커(ticker)의 주가 데이터(OHLCV)를 기간 범위로 조회하는 툴.
# 설정된 core_stock_apis 공급자(vendor)를 사용한다.
# 아래 docstring은 LLM에게 툴 설명으로 그대로 전달되므로 영어 원문을 유지한다.
@tool
def get_stock_data(
    symbol: Annotated[str, "ticker symbol of the company"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
) -> str:
    """
    Retrieve stock price data (OHLCV) for a given ticker symbol.
    Uses the configured core_stock_apis vendor.
    Args:
        symbol (str): Ticker symbol of the company, e.g. AAPL, TSM
        start_date (str): Start date in yyyy-mm-dd format
        end_date (str): End date in yyyy-mm-dd format
    Returns:
        str: A formatted dataframe containing the stock price data for the specified ticker symbol in the specified date range.
    """
    return route_to_vendor("get_stock_data", symbol, start_date, end_date)
