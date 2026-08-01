# 이 파일은 Alpha Vantage 경로의 fail-closed(안전 우선 차단) 전환을 검증하는
# 테스트 모음입니다 (설계 분석 단기 #6). 오류 JSON("Error Message")이 정상
# 데이터처럼 통과하지 않는지, 날짜 필터 실패 시 무필터 통과 대신 예외가
# 발생하는지, OHLCV에 yfinance 경로와 동일한 staleness(진부 데이터) 검사가
# 적용되는지 확인합니다.
"""Alpha Vantage fail-closed 테스트 (설계분석-보고서 2.5절 단기 #6).

기존의 3중 fail-open 구멍에 대한 회귀 방지:
1. "Error Message" JSON이 정상 데이터처럼 통과 -> 타입 있는 오류로 매핑
2. 날짜 필터 실패 시 원본 그대로 반환 -> 예외 발생
3. AV OHLCV에 staleness 검사 부재 -> yfinance와 동일한 stale guard 적용
"""
from __future__ import annotations

import pytest

import tradingagents.dataflows.alpha_vantage_common as av
import tradingagents.dataflows.alpha_vantage_stock as avs
from tradingagents.dataflows.errors import (
    NoMarketDataError,
    VendorError,
    VendorRateLimitError,
)


class _FakeResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


def _patched_get(body):
    def fake_get(url, params=None, **kwargs):
        return _FakeResponse(body)
    return fake_get


# ---------------------------------------------------------------------------
# 1. 오류 JSON 감지
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestErrorJsonDetection:
    def test_error_message_maps_to_no_market_data(self, monkeypatch):
        """"Error Message" 응답이 정상 데이터 대신 NoMarketDataError로 매핑되는지 검증하는 테스트."""
        body = ('{"Error Message": "Invalid API call. Please retry or visit the '
                'documentation for TIME_SERIES_DAILY_ADJUSTED."}')
        monkeypatch.setattr(av.requests, "get", _patched_get(body))
        with pytest.raises(NoMarketDataError) as ctx:
            av._make_api_request("TIME_SERIES_DAILY_ADJUSTED", {"symbol": "BADSYM"})
        assert "BADSYM" in str(ctx.value)
        assert "Invalid API call" in str(ctx.value)

    def test_note_rate_limit_still_detected(self, monkeypatch):
        """"Note" 형식의 요청 한도 안내가 계속 rate limit으로 분류되는지 검증하는 테스트."""
        body = '{"Note": "API call frequency is 5 calls per minute."}'
        monkeypatch.setattr(av.requests, "get", _patched_get(body))
        with pytest.raises(VendorRateLimitError):
            av._make_api_request("TIME_SERIES_DAILY", {"symbol": "AAPL"})

    def test_unclassified_notice_only_body_fails_closed(self, monkeypatch):
        """분류되지 않은 공지만 담긴 응답이 정상 데이터로 통과하지 않는지 검증하는 테스트."""
        body = '{"Information": "Thank you for using Alpha Vantage!"}'
        monkeypatch.setattr(av.requests, "get", _patched_get(body))
        with pytest.raises(VendorError):
            av._make_api_request("TIME_SERIES_DAILY", {"symbol": "AAPL"})

    def test_real_data_with_extra_keys_passes(self, monkeypatch):
        """실제 데이터 키가 있는 JSON 응답은 그대로 통과하는지 검증하는 테스트."""
        body = '{"items": "2", "feed": []}'
        monkeypatch.setattr(av.requests, "get", _patched_get(body))
        assert av._make_api_request("NEWS_SENTIMENT", {"tickers": "AAPL"}) == body

    def test_csv_body_passes(self, monkeypatch):
        """CSV(비 JSON) 응답 본문은 그대로 통과하는지 검증하는 테스트."""
        body = "timestamp,close\n2026-01-02,1.0"
        monkeypatch.setattr(av.requests, "get", _patched_get(body))
        assert av._make_api_request("TIME_SERIES_DAILY", {"symbol": "AAPL"}) == body


# ---------------------------------------------------------------------------
# 2. 날짜 필터 실패 시 예외
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestDateFilterFailsClosed:
    def test_unparseable_csv_raises_instead_of_passing_through(self):
        """필터를 적용할 수 없는 CSV가 원본 그대로 반환되지 않고 예외를 내는지 검증하는 테스트."""
        garbage = "colA,colB\nnot-a-date,1.0"
        with pytest.raises(VendorError):
            av._filter_csv_by_date_range(garbage, "2026-01-01", "2026-01-10")

    def test_valid_csv_still_filters_future_rows(self):
        """정상 CSV에서 요청 범위 밖(미래) 행이 계속 걸러지는지 검증하는 테스트."""
        csv_data = (
            "timestamp,close\n"
            "2026-01-05,1.0\n"
            "2026-06-01,2.0\n"  # end_date 이후(미래) -> 제거돼야 함
        )
        out = av._filter_csv_by_date_range(csv_data, "2026-01-01", "2026-01-10")
        assert "2026-01-05" in out
        assert "2026-06-01" not in out

    def test_empty_body_passes_through(self):
        """빈 본문은 예외 없이 그대로 반환되는지(기존 동작 유지) 검증하는 테스트."""
        assert av._filter_csv_by_date_range("", "2026-01-01", "2026-01-10") == ""


# ---------------------------------------------------------------------------
# 3. OHLCV staleness 검사
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestOhlcvStalenessGuard:
    def _csv(self, *dates):
        rows = "\n".join(f"{d},1.0,1.1,0.9,1.0,1.0,100,0.0,1.0" for d in dates)
        return (
            "timestamp,open,high,low,close,adjusted_close,volume,"
            "dividend_amount,split_coefficient\n" + rows
        )

    def test_stale_frame_rejected(self, monkeypatch):
        """최신 행이 1년 묵은 프레임이 정상 데이터처럼 반환되지 않는지 검증하는 테스트."""
        stale_csv = self._csv("2025-06-11")  # 요청 end_date보다 1년 이상 과거
        monkeypatch.setattr(avs, "_make_api_request", lambda fn, params: stale_csv)
        with pytest.raises(NoMarketDataError) as ctx:
            avs.get_stock("CB", "2026-06-01", "2026-06-11")
        assert "stale" in str(ctx.value)

    def test_fresh_frame_accepted(self, monkeypatch):
        """요청 범위 안의 신선한 데이터는 그대로 반환되는지 검증하는 테스트."""
        fresh_csv = self._csv("2026-06-10", "2026-06-11")
        monkeypatch.setattr(avs, "_make_api_request", lambda fn, params: fresh_csv)
        out = avs.get_stock("CB", "2026-06-01", "2026-06-11")
        assert "2026-06-10" in out
        assert "2026-06-11" in out

    def test_future_rows_filtered_before_staleness_check(self, monkeypatch):
        """미래 행이 걸러진 뒤에도 남은 데이터가 신선하면 통과하는지 검증하는 테스트."""
        mixed_csv = self._csv("2026-06-10", "2026-07-01")  # 2026-07-01은 미래 행
        monkeypatch.setattr(avs, "_make_api_request", lambda fn, params: mixed_csv)
        out = avs.get_stock("CB", "2026-06-01", "2026-06-11")
        assert "2026-06-10" in out
        assert "2026-07-01" not in out  # 룩어헤드 차단 유지
