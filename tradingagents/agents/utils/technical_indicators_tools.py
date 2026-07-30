# =============================================================================
# [모듈 개요 — 초보자용]
# 이 파일은 기술적 지표(technical indicator, 예: RSI, MACD, 이동평균) 분석 리포트를
# 가져오는 LangChain 툴(tool)을 정의합니다. TradingAgents 시스템에서
# 시장 분석가(Market Analyst) 에이전트가 차트 분석에 사용하며, 실제 계산/조회는
# route_to_vendor()가 설정된 technical_indicators 공급자(vendor)로 라우팅합니다.
# =============================================================================

from typing import Annotated

from langchain_core.tools import tool

from tradingagents.dataflows.interface import route_to_vendor


# [한국어 설명] 지정한 티커의 기술적 지표 1개를 조회하는 툴. LLM이 실수로 여러 지표를
# 쉼표로 묶어 전달하는 경우를 대비해 내부에서 분리 처리한다.
# 아래 docstring은 LLM에게 툴 설명으로 전달되므로 영어 원문을 유지한다.
@tool
def get_indicators(
    symbol: Annotated[str, "ticker symbol of the company"],
    indicator: Annotated[str, "technical indicator to get the analysis and report of"],
    curr_date: Annotated[str, "The current trading date you are trading on, YYYY-mm-dd"],
    look_back_days: Annotated[int, "how many days to look back"] = 30,
) -> str:
    """
    Retrieve a single technical indicator for a given ticker symbol.
    Uses the configured technical_indicators vendor.
    Args:
        symbol (str): Ticker symbol of the company, e.g. AAPL, TSM
        indicator (str): A single technical indicator name, e.g. 'rsi', 'macd'. Call this tool once per indicator.
        curr_date (str): The current trading date you are trading on, YYYY-mm-dd
        look_back_days (int): How many days to look back, default is 30
    Returns:
        str: A formatted dataframe containing the technical indicators for the specified ticker symbol and indicator.
    """
    # LLM이 가끔 여러 지표를 쉼표로 구분한 문자열 하나로 전달하므로,
    # 분리해서 각 지표를 개별적으로 처리한다.
    indicators = [i.strip().lower() for i in indicator.split(",") if i.strip()]
    results = []
    for ind in indicators:
        try:
            results.append(route_to_vendor("get_indicators", symbol, ind, curr_date, look_back_days))
        except ValueError as e:
            results.append(str(e))
    return "\n\n".join(results)
