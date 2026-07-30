# =============================================================================
# [모듈 개요 - 초보자용]
# 이 파일은 TradingAgents 프레임워크의 데이터 수집 기능 중 하나인
# 기술적 지표(technical indicator) 조회 함수를 단독으로 테스트하는 스크립트입니다.
# yfinance 기반 데이터 흐름(dataflows)에서 AAPL 종목의 MACD 지표를
# 30일 조회 기간(lookback)으로 가져와, 실행 시간과 결과를 출력합니다.
# 에이전트 전체를 돌리지 않고 데이터 계층만 빠르게 점검할 때 사용합니다.
# =============================================================================

import time

from tradingagents.dataflows.y_finance import (
    get_stock_stats_indicators_window,
)

print("Testing optimized implementation with 30-day lookback:")
start_time = time.time()
result = get_stock_stats_indicators_window("AAPL", "macd", "2024-11-01", 30)
end_time = time.time()

print(f"Execution time: {end_time - start_time:.2f} seconds")
print(f"Result length: {len(result)} characters")
print(result)
