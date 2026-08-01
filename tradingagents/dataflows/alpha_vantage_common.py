# =============================================================================
# [모듈 개요 - 초보자용 안내]
# 이 파일은 Alpha Vantage API 호출에 필요한 공통 기능(유틸리티)을 모아 둔 모듈입니다.
# API 키(key) 조회, 날짜 형식 변환, 실제 HTTP 요청 수행, 요청 한도 초과(rate limit)
# 감지, 날짜 범위로 CSV 데이터 필터링 등을 담당합니다.
# TradingAgents(LLM 멀티 에이전트 주식 트레이딩 프레임워크)의 다른 Alpha Vantage
# 모듈들(주가, 뉴스, 재무제표, 기술적 지표)은 모두 이 모듈의 _make_api_request 를
# 통해 API를 호출합니다.
# =============================================================================
import json
import os
from datetime import datetime
from io import StringIO

import pandas as pd
import requests

from .errors import (
    NoMarketDataError,
    VendorError,
    VendorNotConfiguredError,
    VendorRateLimitError,
)

API_BASE_URL = "https://www.alphavantage.co/query"

# 네트워크 타임아웃(timeout, 초 단위). 응답이 멈춘 Alpha Vantage 요청이
# CLI/에이전트를 무한정 멈추게 하지 않도록 제한한다 (#990).
REQUEST_TIMEOUT = 30


class AlphaVantageNotConfiguredError(VendorNotConfiguredError):
    """Alpha Vantage가 선택되었지만 API 키가 설정되지 않았을 때 발생하는 예외.

    VendorNotConfiguredError(즉, 여전히 ValueError)이기도 하므로, 라우팅 계층의
    "벤더 사용 불가(vendor unavailable)" 처리와 기존에 ValueError를 잡던 호출부가
    모두 그대로 동작한다.
    """
    pass


def get_api_key() -> str:
    """환경변수에서 Alpha Vantage API 키를 가져온다."""
    api_key = os.getenv("ALPHA_VANTAGE_API_KEY")
    if not api_key:
        raise AlphaVantageNotConfiguredError(
            "ALPHA_VANTAGE_API_KEY environment variable is not set."
        )
    return api_key

def format_datetime_for_api(date_input) -> str:
    """다양한 날짜 형식을 Alpha Vantage API가 요구하는 YYYYMMDDTHHMM 형식으로 변환한다."""
    if isinstance(date_input, str):
        # 이미 올바른 형식이면 그대로 반환
        if len(date_input) == 13 and 'T' in date_input:
            return date_input
        # 흔히 쓰이는 날짜 형식들을 순서대로 파싱 시도
        try:
            dt = datetime.strptime(date_input, "%Y-%m-%d")
            return dt.strftime("%Y%m%dT0000")
        except ValueError:
            try:
                dt = datetime.strptime(date_input, "%Y-%m-%d %H:%M")
                return dt.strftime("%Y%m%dT%H%M")
            except ValueError:
                raise ValueError(f"Unsupported date format: {date_input}") from None
    elif isinstance(date_input, datetime):
        return date_input.strftime("%Y%m%dT%H%M")
    else:
        raise ValueError(f"Date must be string or datetime object, got {type(date_input)}")

class AlphaVantageRateLimitError(VendorRateLimitError):
    """Alpha Vantage API 요청 한도(rate limit)를 초과했을 때 발생하는 예외."""
    pass

def _make_api_request(function_name: str, params: dict) -> dict | str:
    """API 요청을 보내고 응답을 처리하는 헬퍼(helper) 함수.

    Raises:
        AlphaVantageRateLimitError: API 요청 한도(rate limit) 초과 시
    """
    # 원본 params 를 수정하지 않도록 복사본을 만든다
    api_params = params.copy()
    api_params.update({
        "function": function_name,
        "apikey": get_api_key(),
        "source": "trading_agents",
    })

    # params 또는 전역 변수에 entitlement(유료 구독 권한) 파라미터가 있으면 처리
    current_entitlement = globals().get('_current_entitlement')
    entitlement = api_params.get("entitlement") or current_entitlement

    if entitlement:
        api_params["entitlement"] = entitlement
    elif "entitlement" in api_params:
        # entitlement 값이 None 이거나 비어 있으면 제거
        api_params.pop("entitlement", None)

    response = requests.get(API_BASE_URL, params=api_params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    response_text = response.text

    # 오류 응답은 JSON 형식이고, 정상 데이터 응답은 보통 CSV(또는 data 키를 가진
    # JSON)다. 즉 JSON 파싱에 실패한 본문은 정상 데이터로 간주하면 된다.
    try:
        response_json = json.loads(response_text)
    except json.JSONDecodeError:
        return response_text

    if not isinstance(response_json, dict):
        return response_text

    # fail-closed: "Error Message"는 잘못된 심볼/함수 호출을 뜻하는 Alpha
    # Vantage의 오류 응답이다. 예전에는 이 JSON이 정상 데이터처럼 통과해
    # LLM 컨텍스트로 흘러갔다. 타입 있는 "데이터 없음" 오류로 바꿔 라우터가
    # 폴백/센티널 처리를 하게 한다.
    error_message = response_json.get("Error Message")
    if error_message:
        symbol_hint = str(
            params.get("symbol") or params.get("tickers") or function_name
        )
        raise NoMarketDataError(
            symbol_hint, detail=f"Alpha Vantage error: {error_message}"
        )

    # Alpha Vantage는 문제를 "Information" / "Note" 필드로 알려준다. 진짜 요청 한도
    # 초과(rate limit)와 잘못된/누락된 API 키가 뒤섞이지 않도록 분류한다 (#991):
    # 한도 초과 안내문에도 "API key"라는 문구가 등장하기 때문에("your API key ...
    # 25 requests per day") 한도 초과 문구를 먼저 검사한다.
    notice = response_json.get("Information") or response_json.get("Note")
    if notice:
        low = notice.lower()
        if any(m in low for m in ("rate limit", "requests per day", "call frequency", "premium")):
            raise AlphaVantageRateLimitError(f"Alpha Vantage rate limit exceeded: {notice}")
        if "api key" in low or "apikey" in low:
            # 기존의 "설정 안 됨(not configured)" 예외를 재사용해, 잘못된 키가
            # 요청 한도 초과로 잘못 표시되는 대신 실제로 조치 가능한 실패로
            # 드러나게 한다 (#991).
            raise AlphaVantageNotConfiguredError(f"Alpha Vantage API key invalid or missing: {notice}")
        # fail-closed: 위 분류에 걸리지 않은 공지(notice)라도, 실제 데이터 키가
        # 하나도 없는 응답이라면 정상 데이터처럼 통과시키지 않는다 — 공지
        # 본문이 LLM 컨텍스트에 데이터로 유입되는 것을 막는다.
        if not (set(response_json) - {"Information", "Note"}):
            raise VendorError(
                f"Alpha Vantage returned a notice instead of data: {notice}"
            )

    return response_text



def _filter_csv_by_date_range(csv_data: str, start_date: str, end_date: str) -> str:
    """
    CSV 데이터에서 지정한 날짜 범위 안의 행만 남기도록 필터링한다.

    (초보자 설명) 백테스트(backtest, 과거 데이터로 전략을 검증하는 것)에서는
    "그 시점에 알 수 있었던 데이터"만 사용해야 한다. API가 요청 범위 밖의 데이터를
    함께 돌려줄 수 있으므로, 여기서 날짜 범위를 다시 한번 잘라내어 미래 데이터가
    섞이는 룩어헤드 편향(look-ahead bias)을 막는다.

    Args:
        csv_data: Alpha Vantage API가 반환한 CSV 문자열
        start_date: 시작 날짜, yyyy-mm-dd 형식
        end_date: 종료 날짜, yyyy-mm-dd 형식

    Returns:
        필터링된 CSV 문자열
    """
    if not csv_data or csv_data.strip() == "":
        return csv_data

    try:
        # CSV 데이터 파싱
        df = pd.read_csv(StringIO(csv_data))

        # 첫 번째 열이 날짜 열(timestamp)이라고 가정한다
        date_col = df.columns[0]
        df[date_col] = pd.to_datetime(df[date_col])

        # 날짜 범위로 필터링
        start_dt = pd.to_datetime(start_date)
        end_dt = pd.to_datetime(end_date)

        filtered_df = df[(df[date_col] >= start_dt) & (df[date_col] <= end_dt)]

        # 다시 CSV 문자열로 변환
        return filtered_df.to_csv(index=False)

    except Exception as e:
        # fail-closed: 예전에는 필터 실패 시 원본을 그대로 반환했는데, 그러면
        # 미래 행(look-ahead)이 걸러지지 않은 채 통과합니다. 필터를 적용할 수
        # 없는 데이터는 사용하지 않고 예외를 던져 라우터가 처리하게 합니다.
        raise VendorError(
            f"Failed to apply date-range filter ({start_date}..{end_date}) "
            f"to Alpha Vantage CSV data: {e}"
        ) from e
