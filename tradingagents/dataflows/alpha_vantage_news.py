# =============================================================================
# [모듈 개요 - 초보자용 안내]
# 이 파일은 Alpha Vantage API에서 뉴스·시장 심리(sentiment) 데이터와 내부자 거래
# (insider transactions) 내역을 가져오는 모듈입니다.
# TradingAgents(LLM 멀티 에이전트 주식 트레이딩 프레임워크)에서 뉴스 애널리스트와
# 소셜/심리 애널리스트 에이전트가 종목 관련 뉴스, 전체 시장 뉴스, 임원·대주주의
# 주식 매매 내역을 분석할 때 이 모듈을 사용합니다.
# =============================================================================
import json

from .alpha_vantage_common import _make_api_request, format_datetime_for_api
from .errors import VendorError


def get_news(ticker, start_date, end_date) -> dict[str, str] | str:
    """전 세계 주요 언론사의 실시간·과거 시장 뉴스와 심리(sentiment) 데이터를 반환한다.

    주식, 암호화폐(cryptocurrency), 외환(forex)은 물론 재정 정책, 인수합병(M&A),
    기업공개(IPO) 같은 주제도 다룬다.

    Args:
        ticker: 뉴스 기사를 검색할 종목 심볼.
        start_date: 뉴스 검색 시작 날짜.
        end_date: 뉴스 검색 종료 날짜.

    Returns:
        뉴스 심리 데이터가 담긴 딕셔너리 또는 JSON 문자열.
    """

    params = {
        "tickers": ticker,
        "time_from": format_datetime_for_api(start_date),
        "time_to": format_datetime_for_api(end_date),
    }

    return _make_api_request("NEWS_SENTIMENT", params)

def get_global_news(curr_date, look_back_days: int = 7, limit: int = 50) -> dict[str, str] | str:
    """특정 종목에 국한하지 않은 글로벌 시장 뉴스·심리(sentiment) 데이터를 반환한다.

    금융 시장, 거시 경제 등 폭넓은 시장 주제를 다룬다.

    Args:
        curr_date: 현재 날짜, yyyy-mm-dd 형식.
        look_back_days: 과거 며칠까지 조회할지(기본값 7).
        limit: 최대 기사 개수(기본값 50).

    Returns:
        글로벌 뉴스 심리 데이터가 담긴 딕셔너리 또는 JSON 문자열.
    """
    from datetime import datetime, timedelta

    # 시작 날짜 계산 (현재 날짜에서 look_back_days 만큼 거슬러 올라감)
    curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    start_dt = curr_dt - timedelta(days=look_back_days)
    start_date = start_dt.strftime("%Y-%m-%d")

    params = {
        "topics": "financial_markets,economy_macro,economy_monetary",
        "time_from": format_datetime_for_api(start_date),
        "time_to": format_datetime_for_api(curr_date),
        "limit": str(limit),
    }

    return _make_api_request("NEWS_SENTIMENT", params)


def _filter_insider_transactions_by_date(result, curr_date: str | None, symbol: str):
    """curr_date 이후의 내부자 거래 항목을 제거해 룩어헤드(look-ahead)를 방지한다.

    Alpha Vantage의 INSIDER_TRANSACTIONS 응답은 ``{"data": [{"transaction_date":
    "YYYY-MM-DD", ...}, ...]}`` 형태의 JSON 문자열입니다. ``curr_date`` 가 주어지면
    ``transaction_date > curr_date`` 인 항목(백테스트 기준 미래의 매매 내역)을
    걸러냅니다. 날짜가 없는 항목은 미래가 아니라고 증명할 수 없으므로 함께
    제외합니다(fail-closed). 필터를 적용할 수 없는 본문이면 조용히 통과시키는
    대신 예외를 던져 라우터가 처리하게 합니다.
    """
    if not curr_date or not isinstance(result, str):
        return result
    try:
        payload = json.loads(result)
    except json.JSONDecodeError:
        raise VendorError(
            f"Cannot enforce look-ahead filter on Alpha Vantage insider "
            f"transactions for {symbol!r}: response body is not JSON"
        ) from None
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return result
    # transaction_date는 ISO(YYYY-MM-DD) 형식이므로 문자열 비교로 충분하다.
    payload["data"] = [
        r for r in payload["data"]
        if isinstance(r, dict)
        and r.get("transaction_date")
        and str(r["transaction_date"]) <= curr_date
    ]
    return json.dumps(payload)


def get_insider_transactions(symbol: str, curr_date: str = None) -> dict[str, str] | str:
    """주요 이해관계자의 최신·과거 내부자 거래(insider transactions) 내역을 반환한다.

    창업자, 임원, 이사회 구성원 등의 주식 매매 내역을 다룬다.

    Args:
        symbol: 티커 심볼(ticker symbol). 예: "IBM".
        curr_date: 현재 트레이딩 날짜(yyyy-mm-dd). 지정하면 이 날짜 이후의
            거래를 걸러내 백테스트의 룩어헤드를 방지한다.

    Returns:
        내부자 거래 데이터가 담긴 딕셔너리 또는 JSON 문자열.
    """

    params = {
        "symbol": symbol,
    }

    result = _make_api_request("INSIDER_TRANSACTIONS", params)
    return _filter_insider_transactions_by_date(result, curr_date, symbol)
