# =============================================================================
# [모듈 개요 — 초보자용]
# 이 파일은 기업의 기본적 분석(fundamental analysis) 데이터를 가져오는 LangChain 툴(tool)
# 모음입니다: 종합 펀더멘털, 재무상태표(balance sheet), 현금흐름표(cash flow statement),
# 손익계산서(income statement). TradingAgents 시스템에서 펀더멘털 분석가(Fundamentals
# Analyst) 에이전트가 LLM 툴 호출로 이 함수들을 실행하며, 실제 데이터 조회는
# route_to_vendor()가 설정된 fundamental_data 공급자(vendor)로 라우팅합니다.
# =============================================================================

from typing import Annotated

from langchain_core.tools import tool

from tradingagents.dataflows.interface import route_to_vendor


# [한국어 설명] 지정한 티커의 종합 펀더멘털(기본적 분석) 데이터를 조회하는 툴.
# 아래 docstring은 LLM에게 툴 설명으로 전달되므로 영어 원문을 유지한다.
@tool
def get_fundamentals(
    ticker: Annotated[str, "ticker symbol"],
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"],
) -> str:
    """
    Retrieve comprehensive fundamental data for a given ticker symbol.
    Uses the configured fundamental_data vendor.
    Args:
        ticker (str): Ticker symbol of the company
        curr_date (str): Current date you are trading at, yyyy-mm-dd
    Returns:
        str: A formatted report containing comprehensive fundamental data
    """
    return route_to_vendor("get_fundamentals", ticker, curr_date)


# [한국어 설명] 지정한 티커의 재무상태표(balance sheet) 데이터를 조회하는 툴.
# freq로 연간(annual)/분기(quarterly) 보고 주기를 선택한다(기본값 quarterly).
# 아래 docstring은 LLM에게 툴 설명으로 전달되므로 영어 원문을 유지한다.
@tool
def get_balance_sheet(
    ticker: Annotated[str, "ticker symbol"],
    freq: Annotated[str, "reporting frequency: annual/quarterly"] = "quarterly",
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"] = None,
) -> str:
    """
    Retrieve balance sheet data for a given ticker symbol.
    Uses the configured fundamental_data vendor.
    Args:
        ticker (str): Ticker symbol of the company
        freq (str): Reporting frequency: annual/quarterly (default quarterly)
        curr_date (str): Current date you are trading at, yyyy-mm-dd
    Returns:
        str: A formatted report containing balance sheet data
    """
    return route_to_vendor("get_balance_sheet", ticker, freq, curr_date)


# [한국어 설명] 지정한 티커의 현금흐름표(cash flow statement) 데이터를 조회하는 툴.
# 아래 docstring은 LLM에게 툴 설명으로 전달되므로 영어 원문을 유지한다.
@tool
def get_cashflow(
    ticker: Annotated[str, "ticker symbol"],
    freq: Annotated[str, "reporting frequency: annual/quarterly"] = "quarterly",
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"] = None,
) -> str:
    """
    Retrieve cash flow statement data for a given ticker symbol.
    Uses the configured fundamental_data vendor.
    Args:
        ticker (str): Ticker symbol of the company
        freq (str): Reporting frequency: annual/quarterly (default quarterly)
        curr_date (str): Current date you are trading at, yyyy-mm-dd
    Returns:
        str: A formatted report containing cash flow statement data
    """
    return route_to_vendor("get_cashflow", ticker, freq, curr_date)


# [한국어 설명] 지정한 티커의 손익계산서(income statement) 데이터를 조회하는 툴.
# 아래 docstring은 LLM에게 툴 설명으로 전달되므로 영어 원문을 유지한다.
@tool
def get_income_statement(
    ticker: Annotated[str, "ticker symbol"],
    freq: Annotated[str, "reporting frequency: annual/quarterly"] = "quarterly",
    curr_date: Annotated[str, "current date you are trading at, yyyy-mm-dd"] = None,
) -> str:
    """
    Retrieve income statement data for a given ticker symbol.
    Uses the configured fundamental_data vendor.
    Args:
        ticker (str): Ticker symbol of the company
        freq (str): Reporting frequency: annual/quarterly (default quarterly)
        curr_date (str): Current date you are trading at, yyyy-mm-dd
    Returns:
        str: A formatted report containing income statement data
    """
    return route_to_vendor("get_income_statement", ticker, freq, curr_date)
