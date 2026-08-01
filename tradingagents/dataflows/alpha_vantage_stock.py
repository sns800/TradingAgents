# =============================================================================
# [모듈 개요 - 초보자용 안내]
# 이 파일은 Alpha Vantage API에서 일별 주가 데이터(OHLCV: 시가·고가·저가·종가·
# 거래량)와 수정 종가(adjusted close), 액면분할·배당 이력을 가져오는 모듈입니다.
# TradingAgents(LLM 멀티 에이전트 주식 트레이딩 프레임워크)에서 마켓 애널리스트
# 에이전트가 주가 흐름을 분석할 때 사용하는 가장 기본적인 데이터 소스입니다.
# =============================================================================
from datetime import datetime
from io import StringIO

import pandas as pd

from .alpha_vantage_common import _filter_csv_by_date_range, _make_api_request
from .stockstats_utils import _assert_ohlcv_not_stale


def get_stock(
    symbol: str,
    start_date: str,
    end_date: str
) -> str:
    """
    지정한 날짜 범위로 필터링된 일별 OHLCV 원시 값, 수정 종가(adjusted close),
    과거 액면분할/배당 이벤트를 반환한다.

    Args:
        symbol: 종목(주식) 이름. 예: symbol=IBM
        start_date: 시작 날짜, yyyy-mm-dd 형식
        end_date: 종료 날짜, yyyy-mm-dd 형식

    Returns:
        날짜 범위로 필터링된 일별 수정 시계열 데이터가 담긴 CSV 문자열.
    """
    # 날짜를 파싱해 요청 범위를 파악한다
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    today = datetime.now()

    # 요청 범위가 최근 100일 이내인지에 따라 outputsize 를 선택한다.
    # "compact"는 최신 100개 데이터만 반환하므로(응답이 작고 빠름),
    # start_date가 충분히 최근일 때만 사용할 수 있는지 확인한다.
    days_from_today_to_start = (today - start_dt).days
    outputsize = "compact" if days_from_today_to_start < 100 else "full"

    params = {
        "symbol": symbol,
        "outputsize": outputsize,
        "datatype": "csv",
    }

    response = _make_api_request("TIME_SERIES_DAILY_ADJUSTED", params)

    # yfinance 경로와 동일한 진부(stale) 데이터 거부(#1021 패턴): 벤더가 가진
    # 최신 행이 요청한 end_date보다 지나치게 오래됐다면(예: 상장폐지, 커버리지
    # 중단) 아래 범위 필터가 조용히 빈 결과를 만들기 전에 명시적으로 거부한다.
    # 반드시 범위 필터 *이전*의 전체 응답으로 검사한다 — 필터 후에는 진부한
    # 행이 범위 밖으로 걸러져 검사가 무력화되기 때문이다. Alpha Vantage CSV의
    # 첫 컬럼(timestamp)을 검사용 Date 컬럼으로 이름만 바꿔 공용 가드에 넘긴다.
    if isinstance(response, str) and response.strip():
        try:
            frame = pd.read_csv(StringIO(response))
        except Exception:
            # CSV로 파싱할 수 없는 본문은 아래 범위 필터가 fail-closed로
            # 예외 처리한다(빈 프레임 정책도 가드와 동일: 데이터-없음 처리에 맡김).
            frame = None
        if frame is not None and not frame.empty:
            _assert_ohlcv_not_stale(
                frame.rename(columns={frame.columns[0]: "Date"}), end_date, symbol
            )

    # 요청 범위 밖(특히 end_date 이후 = 미래)의 행을 잘라내어
    # 백테스트 시 룩어헤드 편향(look-ahead bias)을 방지한다
    return _filter_csv_by_date_range(response, start_date, end_date)
