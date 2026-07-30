# =============================================================================
# [모듈 개요 — 초보자용]
# 이 파일은 거시경제 지표(macro indicator) 시계열을 FRED(미국 연방준비은행 경제 데이터,
# Federal Reserve Economic Data)에서 가져오는 LangChain 툴(tool)을 정의합니다.
# TradingAgents 시스템에서 뉴스/거시 분석 담당 에이전트가 금리, 물가(CPI), 실업률 등
# 경제 지표를 조회할 때 사용하며, 실제 조회는 route_to_vendor()가 설정된
# macro_data 공급자(vendor)로 라우팅합니다.
# =============================================================================

from typing import Annotated

from langchain_core.tools import tool

from tradingagents.dataflows.interface import route_to_vendor


# [한국어 설명] FRED에서 거시경제 지표 시계열을 조회하는 툴. 지표는 'cpi',
# 'fed_funds_rate' 같은 별칭(alias) 또는 'CPIAUCSL' 같은 원시 FRED 시리즈 ID로
# 지정한다. 시리즈 제목, 단위, 주기, 최신값, 기간 내 변화, 최근 관측치 표를 반환한다.
# 아래 docstring은 LLM에게 툴 설명으로 전달되므로 영어 원문을 유지한다.
@tool
def get_macro_indicators(
    indicator: Annotated[
        str,
        "Macro indicator: a friendly alias such as 'cpi', 'core_pce', "
        "'unemployment', 'fed_funds_rate', '10y_treasury', 'yield_curve', "
        "'real_gdp', 'vix', or a raw FRED series ID such as 'CPIAUCSL'.",
    ],
    curr_date: Annotated[str, "Current date in yyyy-mm-dd format; the end of the window"],
    look_back_days: Annotated[
        int | None, "Trailing window length in days; omit for a 1-year window"
    ] = None,
) -> str:
    """
    Retrieve a macroeconomic indicator time series from FRED (Federal Reserve
    Economic Data): policy rates, Treasury yields, inflation, labor, and growth.
    Returns the series title, units, frequency, the latest value, the change
    over the window, and a recent observation table. Uses the configured
    macro_data vendor.

    Args:
        indicator (str): Friendly alias or raw FRED series ID
        curr_date (str): Current date in yyyy-mm-dd format
        look_back_days (int): Trailing window length; omit for a 1-year window

    Returns:
        str: A formatted markdown report of the macro series
    """
    return route_to_vendor("get_macro_indicators", indicator, curr_date, look_back_days)
