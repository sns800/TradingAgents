# 이 파일은 Alpha Vantage(주가 데이터 API) 요청 처리의 안정성 강화를 검증하는
# 테스트 모음입니다. 타임아웃 누락, 오류 응답 오분류, 미래 날짜 데이터 누출 같은
# 과거 버그가 다시 발생하지 않는지(회귀 방지) 확인합니다.
"""Alpha Vantage 요청 안정성 강화(hardening) 테스트.

다음 이슈들의 회귀(regression) 방지용:
#990 (요청 타임아웃이 없어 응답이 늦으면 무한 대기 가능),
#991 (잘못된 API 키 응답이 속도 제한(rate limit)으로 오분류되어
일시적 오류로 조용히 처리되던 문제),
#1115 (재무제표 응답이 dict가 아닌 JSON 문자열이라 미래 데이터
차단(look-ahead) 필터가 아예 실행되지 않던 문제).
"""
import json

import pytest

import tradingagents.dataflows.alpha_vantage_common as av
import tradingagents.dataflows.alpha_vantage_fundamentals as avf


class _FakeResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


def _patched_get(body, capture=None):
    def fake_get(url, params=None, **kwargs):
        if capture is not None:
            capture.update(kwargs)
        return _FakeResponse(body)
    return fake_get


@pytest.mark.unit
def test_request_passes_timeout(monkeypatch):
    """API 요청에 타임아웃(timeout)이 반드시 전달되는지 검증하는 테스트 (#990).

    타임아웃이 없으면 서버 무응답 시 프로그램이 무한 대기할 수 있습니다.
    """
    captured = {}
    monkeypatch.setattr(av.requests, "get", _patched_get("Date,Close\n2025-01-02,1.0", captured))
    av._make_api_request("TIME_SERIES_DAILY", {"symbol": "AAPL"})
    assert captured.get("timeout") == av.REQUEST_TIMEOUT  # #990


@pytest.mark.unit
def test_rate_limit_detected(monkeypatch):
    """속도 제한(rate limit) 응답을 전용 예외로 감지하는지 검증하는 테스트."""
    body ='{"Information": "Our standard API rate limit is 25 requests per day. ... your API key ..."}'
    monkeypatch.setattr(av.requests, "get", _patched_get(body))
    with pytest.raises(av.AlphaVantageRateLimitError):
        av._make_api_request("TIME_SERIES_DAILY", {"symbol": "AAPL"})


@pytest.mark.unit
def test_invalid_key_not_mislabeled_as_rate_limit(monkeypatch):
    """잘못된 API 키 응답이 속도 제한으로 오분류되지 않는지 검증하는 테스트 (#991)."""
    # Alpha Vantage의 잘못된 키 안내문에도 "API key"라는 문구가 들어 있는데,
    # 이를 (일시적인) 속도 제한으로 취급하면 안 되고,
    # 진짜 설정 오류(configuration error)로 드러나야 합니다 (#991).
    body = ('{"Information": "the parameter apikey is invalid or missing. '
            'Please claim your free API key on (https://www.alphavantage.co/support/#api-key)."}')
    monkeypatch.setattr(av.requests, "get", _patched_get(body))
    with pytest.raises(av.AlphaVantageNotConfiguredError):
        av._make_api_request("TIME_SERIES_DAILY", {"symbol": "AAPL"})
    with pytest.raises(av.AlphaVantageRateLimitError):  # 검증: 속도 제한 경로는 여전히 별도로 구분됨
        monkeypatch.setattr(av.requests, "get", _patched_get('{"Note": "API call frequency is 5 calls per minute."}'))
        av._make_api_request("TIME_SERIES_DAILY", {"symbol": "AAPL"})


_FUNDAMENTALS_JSON = json.dumps({
    "symbol": "AAPL",
    "annualReports": [
        {"fiscalDateEnding": "2025-12-31", "totalAssets": "1"},   # 미래 날짜 -> 제거되어야 함
        {"fiscalDateEnding": "2023-12-31", "totalAssets": "2"},   # 과거 날짜 -> 유지되어야 함
    ],
    "quarterlyReports": [
        {"fiscalDateEnding": "2024-06-30", "totalAssets": "3"},   # 미래 날짜 -> 제거되어야 함
        {"fiscalDateEnding": "2023-09-30", "totalAssets": "4"},   # 과거 날짜 -> 유지되어야 함
    ],
})


@pytest.mark.unit
def test_fundamentals_look_ahead_filter_runs_on_json_string(monkeypatch):
    """JSON 문자열 응답에서도 미래 날짜 필터가 동작하는지 검증하는 테스트 (#1115)."""
    # #1115: 응답 페이로드(payload)는 dict가 아닌 JSON *문자열*로 도착합니다.
    # 예전에는 dict일 때만 필터를 적용해서, 과거 시점 백테스트 실행에
    # 미래 회계 기간 데이터가 누출되는 문제가 있었습니다.
    monkeypatch.setattr(avf, "_make_api_request", lambda fn, params: _FUNDAMENTALS_JSON)
    out = avf.get_balance_sheet("AAPL", curr_date="2024-01-01")
    assert isinstance(out, str)  # 호출자는 여전히 문자열(str)을 받아야 함
    parsed = json.loads(out)
    assert [r["fiscalDateEnding"] for r in parsed["annualReports"]] == ["2023-12-31"]
    assert [r["fiscalDateEnding"] for r in parsed["quarterlyReports"]] == ["2023-09-30"]


@pytest.mark.unit
def test_fundamentals_no_curr_date_passes_through(monkeypatch):
    """기준 날짜(curr_date)가 없으면 응답을 필터링 없이 그대로 통과시키는지 검증하는 테스트."""
    monkeypatch.setattr(avf, "_make_api_request", lambda fn, params: _FUNDAMENTALS_JSON)
    assert avf.get_income_statement("AAPL") == _FUNDAMENTALS_JSON


@pytest.mark.unit
def test_fundamentals_non_json_body_unchanged(monkeypatch):
    """JSON이 아닌 응답 본문은 변형 없이 그대로 반환되는지 검증하는 테스트."""
    monkeypatch.setattr(avf, "_make_api_request", lambda fn, params: "not-json")
    assert avf.get_cashflow("AAPL", curr_date="2024-01-01") == "not-json"
