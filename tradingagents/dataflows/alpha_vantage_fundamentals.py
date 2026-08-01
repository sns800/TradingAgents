# =============================================================================
# [모듈 개요 - 초보자용 안내]
# 이 파일은 Alpha Vantage API에서 기업의 펀더멘털(fundamentals, 기초 재무) 데이터를
# 가져오는 모듈입니다. 기업 개요, 대차대조표(balance sheet), 현금흐름표(cash flow),
# 손익계산서(income statement)를 조회합니다.
# TradingAgents(LLM 멀티 에이전트 주식 트레이딩 프레임워크)에서 펀더멘털 애널리스트
# 에이전트가 기업의 재무 건전성을 분석할 때 이 모듈의 데이터를 사용합니다.
# =============================================================================
import json
from datetime import datetime, timedelta

from .alpha_vantage_common import _make_api_request
from .stockstats_utils import FINANCIALS_DISCLOSURE_LAG_DAYS
from .utils import is_historical_run, snapshot_warning_banner


def _filter_reports_by_date(result, curr_date: str):
    """curr_date 시점에 아직 공시되지 않았을 연간/분기 보고서를 제거해
    룩어헤드(look-ahead)를 방지한다.

    (초보자 설명) 룩어헤드 편향(look-ahead bias)이란 백테스트 시 그 시점에는 아직
    공개되지 않았을 미래 정보를 미리 보는 오류다. 예를 들어 2023년 1월 시점을
    시뮬레이션하면서 2023년 3월 분기 보고서를 참고하면 실제로는 불가능한 "미래를
    아는" 전략이 되어 백테스트 결과가 왜곡된다.

    ``fiscalDateEnding`` 은 회계 기간 종료일이지 공시일이 아니다 — 실제 공시는
    미국 SEC 기준 분기 보고서(10-Q)가 종료 후 40~45일, 연차 보고서(10-K)가
    60~90일 뒤에 이루어지므로, 종료일 + 공시 지연(45일, 10-Q 최대 제출 기한의
    보수적 근사)이 curr_date 이하인 보고서만 남긴다(yfinance 경로의
    ``filter_financials_by_date`` 와 동일 정책).

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
    # 공시 지연을 반영한 컷오프: 종료일 <= curr_date - 45일 인 보고서만
    # 그 시점에 공개돼 있었다고 간주한다. 날짜가 없는(빈) 보고서는 미래가
    # 아니라고 증명할 수 없으므로 함께 제외한다(fail-closed).
    cutoff = (
        datetime.strptime(curr_date, "%Y-%m-%d")
        - timedelta(days=FINANCIALS_DISCLOSURE_LAG_DAYS)
    ).strftime("%Y-%m-%d")
    for key in ("annualReports", "quarterlyReports"):
        if isinstance(payload.get(key), list):
            payload[key] = [
                r for r in payload[key]
                if r.get("fiscalDateEnding") and r["fiscalDateEnding"] <= cutoff
            ]
    return json.dumps(payload)


def get_fundamentals(ticker: str, curr_date: str = None) -> str:
    """
    Alpha Vantage를 이용해 지정한 티커(ticker, 종목 코드)의 종합 펀더멘털 데이터를 가져온다.

    Args:
        ticker (str): 회사의 티커 심볼(ticker symbol)
        curr_date (str): 현재 트레이딩 중인 날짜, yyyy-mm-dd 형식. OVERVIEW는
            시점(point-in-time) 조회를 지원하지 않는 현재 스냅샷이므로, 과거
            날짜면 결과 앞에 경고 배너를 붙인다(yfinance 경로와 동일 정책).

    Returns:
        str: 재무 비율과 핵심 지표를 포함한 기업 개요(overview) 데이터
    """
    params = {
        "symbol": ticker,
    }

    result = _make_api_request("OVERVIEW", params)

    # 데이터 소스가 과거 시점 조회를 지원하지 않으므로 차단 대신 경고한다.
    if isinstance(result, str) and is_historical_run(curr_date):
        result = snapshot_warning_banner(curr_date, "펀더멘털") + result
    return result


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
