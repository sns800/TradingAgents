# =============================================================================
# [모듈 개요 — 초보자용]
# 이 파일은 시장 데이터 검증 스냅샷(verified market snapshot)을 제공하는 LangChain
# 툴(tool)을 정의합니다. LLM이 가격, RSI, 이동평균(moving average) 같은 수치를
# "지어내는"(환각, hallucination) 것을 막기 위해, 에이전트가 정확한 수치를 주장하기 전에
# 이 툴을 호출해 결정론적(deterministic)으로 계산된 실제 값을 기준(source of truth)으로
# 삼도록 합니다. TradingAgents 시스템의 시장 분석 단계에서 사용됩니다.
# =============================================================================

from typing import Annotated

from langchain_core.tools import tool

from tradingagents.dataflows.market_data_validator import build_verified_market_snapshot


# [한국어 설명] 정확한 시장 데이터 주장을 검증하기 위한 결정론적 스냅샷 툴.
# curr_date 이전(포함) 최신 OHLCV 행, 주요 기술적 지표(technical indicators),
# 최근 종가 목록을 반환한다. 가격 수준, 볼린저 밴드(Bollinger bands), RSI, MACD,
# 이동평균, 지지/저항(support/resistance) 등 정확한 수치를 언급하기 전에 호출해야 한다.
# 아래 docstring은 LLM에게 툴 설명으로 전달되므로 영어 원문을 유지한다.
@tool
def get_verified_market_snapshot(
    symbol: Annotated[str, "ticker symbol of the company"],
    curr_date: Annotated[str, "the current trading date, YYYY-mm-dd"],
    look_back_days: Annotated[
        int, "number of recent trading rows to include for sanity-checking"
    ] = 30,
) -> str:
    """Deterministic verification snapshot for exact market-data claims.

    Returns the latest OHLCV row on or before curr_date, common technical
    indicators, and recent closes. Call this before making exact claims about
    price levels, Bollinger bands, RSI, MACD, moving averages, support /
    resistance, or historical comparisons, and treat it as the source of truth.
    """
    return build_verified_market_snapshot(symbol, curr_date, look_back_days)
