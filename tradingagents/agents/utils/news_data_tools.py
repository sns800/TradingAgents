# =============================================================================
# [모듈 개요 — 초보자용]
# 이 파일은 뉴스 관련 데이터를 가져오는 LangChain 툴(tool) 모음입니다:
# 종목별 뉴스, 글로벌(세계) 뉴스, 내부자 거래(insider transactions) 정보.
# TradingAgents 시스템에서 뉴스 분석가(News Analyst)와 소셜/감성 분석가 에이전트가
# LLM 툴 호출로 이 함수들을 실행하며, 실제 조회는 route_to_vendor()가 설정된
# news_data 공급자(vendor)로 라우팅합니다.
# =============================================================================

from typing import Annotated

from langchain_core.tools import tool

from tradingagents.dataflows.interface import route_to_vendor


# [한국어 설명] 지정한 티커의 기간별 뉴스 데이터를 조회하는 툴.
# 아래 docstring은 LLM에게 툴 설명으로 전달되므로 영어 원문을 유지한다.
@tool
def get_news(
    ticker: Annotated[str, "Ticker symbol"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
) -> str:
    """
    Retrieve news data for a given ticker symbol.
    Uses the configured news_data vendor.
    Args:
        ticker (str): Ticker symbol
        start_date (str): Start date in yyyy-mm-dd format
        end_date (str): End date in yyyy-mm-dd format
    Returns:
        str: A formatted string containing news data
    """
    return route_to_vendor("get_news", ticker, start_date, end_date)

# [한국어 설명] 글로벌(세계) 뉴스 데이터를 조회하는 툴. look_back_days와 limit을
# 생략하면 DEFAULT_CONFIG의 기본값(global_news_lookback_days,
# global_news_article_limit)을 사용한다.
# 아래 docstring은 LLM에게 툴 설명으로 전달되므로 영어 원문을 유지한다.
@tool
def get_global_news(
    curr_date: Annotated[str, "Current date in yyyy-mm-dd format"],
    look_back_days: Annotated[int | None, "Days to look back; omit to use the configured default"] = None,
    limit: Annotated[int | None, "Max articles to return; omit to use the configured default"] = None,
) -> str:
    """
    Retrieve global news data.
    Uses the configured news_data vendor. Defaults for look_back_days and
    limit come from DEFAULT_CONFIG (global_news_lookback_days,
    global_news_article_limit); pass explicit values to override.

    Args:
        curr_date (str): Current date in yyyy-mm-dd format
        look_back_days (int): Number of days to look back; omit to inherit config
        limit (int): Maximum number of articles to return; omit to inherit config

    Returns:
        str: A formatted string containing global news data
    """
    return route_to_vendor("get_global_news", curr_date, look_back_days, limit)

# [한국어 설명] 지정한 기업의 내부자 거래(insider transactions) 정보를 조회하는 툴.
# 아래 docstring은 LLM에게 툴 설명으로 전달되므로 영어 원문을 유지한다.
@tool
def get_insider_transactions(
    ticker: Annotated[str, "ticker symbol"],
) -> str:
    """
    Retrieve insider transaction information about a company.
    Uses the configured news_data vendor.
    Args:
        ticker (str): Ticker symbol of the company
    Returns:
        str: A report of insider transaction data
    """
    return route_to_vendor("get_insider_transactions", ticker)
