# =============================================================================
# [모듈 개요 - 초보자용 안내]
# 이 파일은 Alpha Vantage API에서 기업의 펀더멘털(fundamentals, 기초 재무) 데이터를
# 가져오는 모듈입니다. 기업 개요, 대차대조표(balance sheet), 현금흐름표(cash flow),
# 손익계산서(income statement)를 조회합니다.
# TradingAgents(LLM 멀티 에이전트 주식 트레이딩 프레임워크)에서 펀더멘털 애널리스트
# 에이전트가 기업의 재무 건전성을 분석할 때 이 모듈의 데이터를 사용합니다.
# =============================================================================
import json

from .alpha_vantage_common import _make_api_request


def _filter_reports_by_date(result, curr_date: str):
    """curr_date 이후 날짜의 연간/분기 보고서를 제거해 룩어헤드(look-ahead)를 방지한다.

    (초보자 설명) 룩어헤드 편향(look-ahead bias)이란 백테스트 시 그 시점에는 아직
    공개되지 않았을 미래 정보를 미리 보는 오류다. 예를 들어 2023년 1월 시점을
    시뮬레이션하면서 2023년 3월 분기 보고서를 참고하면 실제로는 불가능한 "미래를
    아는" 전략이 되어 백테스트 결과가 왜곡된다. 그래서 curr_date(현재 시뮬레이션
    날짜) 이후의 보고서를 걸러낸다.

    ``_make_api_request`` 는 펀더멘털 응답을 JSON 문자열로 반환하므로, 파싱하고
    필터링한 뒤 다시 직렬화한다. JSON이 아닌 본문이거나 ``curr_date`` 가 지정되지
    않은 경우에는 그대로 반환한다.
    """
    if not curr_date or not isinstance(result, str):
        return result
    try:
        payload = json.loads(result)
    except json.JSONDecodeError:
        return result
    if not isinstance(payload, dict):
        return result
    for key in ("annualReports", "quarterlyReports"):
        if isinstance(payload.get(key), list):
            payload[key] = [
                r for r in payload[key]
                if r.get("fiscalDateEnding", "") <= curr_date
            ]
    return json.dumps(payload)


def get_fundamentals(ticker: str, curr_date: str = None) -> str:
    """
    Alpha Vantage를 이용해 지정한 티커(ticker, 종목 코드)의 종합 펀더멘털 데이터를 가져온다.

    Args:
        ticker (str): 회사의 티커 심볼(ticker symbol)
        curr_date (str): 현재 트레이딩 중인 날짜, yyyy-mm-dd 형식 (Alpha Vantage에서는 사용하지 않음)

    Returns:
        str: 재무 비율과 핵심 지표를 포함한 기업 개요(overview) 데이터
    """
    params = {
        "symbol": ticker,
    }

    return _make_api_request("OVERVIEW", params)


def get_balance_sheet(ticker: str, freq: str = "quarterly", curr_date: str = None):
    """Alpha Vantage를 이용해 지정한 티커의 대차대조표(balance sheet) 데이터를 가져온다."""
    result = _make_api_request("BALANCE_SHEET", {"symbol": ticker})
    return _filter_reports_by_date(result, curr_date)


def get_cashflow(ticker: str, freq: str = "quarterly", curr_date: str = None):
    """Alpha Vantage를 이용해 지정한 티커의 현금흐름표(cash flow statement) 데이터를 가져온다."""
    result = _make_api_request("CASH_FLOW", {"symbol": ticker})
    return _filter_reports_by_date(result, curr_date)


def get_income_statement(ticker: str, freq: str = "quarterly", curr_date: str = None):
    """Alpha Vantage를 이용해 지정한 티커의 손익계산서(income statement) 데이터를 가져온다."""
    result = _make_api_request("INCOME_STATEMENT", {"symbol": ticker})
    return _filter_reports_by_date(result, curr_date)
