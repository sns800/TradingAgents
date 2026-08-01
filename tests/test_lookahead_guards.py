# 이 파일은 펀더멘털·내부자 거래·소셜 감성 경로의 룩어헤드(look-ahead) 차단을
# 검증하는 테스트 모음입니다. 1년 전 curr_date로 각 함수를 호출했을 때
# (1) 미래 데이터가 0건이고 (2) 시점 조회가 불가능한 스냅샷 소스에는
# 경고 배너가 붙는지 확인합니다 (설계 분석 단기 #3).
"""룩어헤드 차단 가드 테스트 (설계분석-보고서 2.5절 단기 #3).

OHLCV(Date<=curr_date)·뉴스(UTC 창)에만 있던 룩어헤드 방지가 다음 경로에도
적용되는지 검증한다:

- 내부자 거래: 두 벤더 모두 transactionDate <= curr_date 필터
- yfinance/Alpha Vantage 펀더멘털: 과거 curr_date 실행 시 스냅샷 경고 배너
- StockTwits/Reddit: 타임스탬프 필터 + 과거 실행 경고 배너
- 재무제표: 회계기간 종료일 + 공시 지연 45일 컷오프
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pandas as pd
import pytest

import tradingagents.dataflows.alpha_vantage_fundamentals as avf
import tradingagents.dataflows.alpha_vantage_news as avn
import tradingagents.dataflows.stockstats_utils as su
import tradingagents.dataflows.y_finance as yfin
from tradingagents.dataflows import reddit, stocktwits
from tradingagents.dataflows.utils import is_historical_run, snapshot_warning_banner

# 1년 전 시점의 백테스트 실행을 시뮬레이션하는 curr_date.
PAST_DATE = (date.today() - timedelta(days=365)).strftime("%Y-%m-%d")
# PAST_DATE 기준으로 "미래"인 날짜 (실제로는 최근 = 데이터 소스가 반환하는 현재 데이터).
TODAY = date.today().strftime("%Y-%m-%d")

BANNER_MARK = "⚠️"


# ---------------------------------------------------------------------------
# 공용 헬퍼 (utils)
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestSnapshotHelpers:
    def test_past_date_is_historical(self):
        """1년 전 날짜가 과거 실행으로 판정되는지 검증하는 테스트."""
        assert is_historical_run(PAST_DATE) is True

    def test_today_is_not_historical(self):
        """오늘 날짜는 실시간 실행으로 판정되는지(배너 없음) 검증하는 테스트."""
        assert is_historical_run(TODAY) is False

    def test_none_and_garbage_are_not_historical(self):
        """curr_date가 없거나 파싱 불가면 배너를 붙이지 않는지 검증하는 테스트."""
        assert is_historical_run(None) is False
        assert is_historical_run("not-a-date") is False

    def test_banner_mentions_curr_date(self):
        """경고 배너에 curr_date와 스냅샷 문구가 포함되는지 검증하는 테스트."""
        banner = snapshot_warning_banner(PAST_DATE, "펀더멘털")
        assert BANNER_MARK in banner
        assert PAST_DATE in banner
        assert "스냅샷" in banner


# ---------------------------------------------------------------------------
# 내부자 거래 — yfinance
# ---------------------------------------------------------------------------
def _fake_insider_ticker(frame):
    class FakeTicker:
        def __init__(self, symbol):
            pass

        @property
        def insider_transactions(self):
            return frame

    return FakeTicker


@pytest.mark.unit
class TestYFinanceInsiderLookahead:
    def _frame(self):
        return pd.DataFrame({
            "Start Date": [
                pd.Timestamp(PAST_DATE) - pd.Timedelta(days=10),
                pd.Timestamp(TODAY),
            ],
            "Insider": ["PAST INSIDER", "FUTURE INSIDER"],
            "Shares": [100, 200],
        })

    def test_future_transactions_filtered(self, monkeypatch):
        """1년 전 curr_date 실행 시 미래 거래가 0건인지 검증하는 테스트."""
        monkeypatch.setattr(
            yfin.yf, "Ticker", _fake_insider_ticker(self._frame())
        )
        out = yfin.get_insider_transactions("AAPL", curr_date=PAST_DATE)
        assert "PAST INSIDER" in out
        assert "FUTURE INSIDER" not in out  # 미래 매매 내역 유입 차단
        assert PAST_DATE in out  # 필터 적용 사실이 헤더에 표기됨

    def test_all_future_yields_no_data_message(self, monkeypatch):
        """모든 거래가 미래면 빈 CSV 대신 명확한 안내를 반환하는지 검증하는 테스트."""
        frame = pd.DataFrame({
            "Start Date": [pd.Timestamp(TODAY)],
            "Insider": ["FUTURE INSIDER"],
            "Shares": [1],
        })
        monkeypatch.setattr(yfin.yf, "Ticker", _fake_insider_ticker(frame))
        out = yfin.get_insider_transactions("AAPL", curr_date=PAST_DATE)
        assert "No insider transactions" in out
        assert "FUTURE INSIDER" not in out

    def test_no_curr_date_keeps_live_behavior(self, monkeypatch):
        """curr_date 미지정(실시간) 시 필터 없이 전체를 반환하는지 검증하는 테스트."""
        monkeypatch.setattr(
            yfin.yf, "Ticker", _fake_insider_ticker(self._frame())
        )
        out = yfin.get_insider_transactions("AAPL")
        assert "PAST INSIDER" in out
        assert "FUTURE INSIDER" in out

    def test_missing_date_column_fails_closed(self, monkeypatch):
        """날짜 컬럼이 없으면 조용히 통과하는 대신 예외를 던지는지 검증하는 테스트."""
        frame = pd.DataFrame({"Insider": ["X"], "Shares": [1]})
        monkeypatch.setattr(yfin.yf, "Ticker", _fake_insider_ticker(frame))
        from tradingagents.dataflows.errors import VendorError
        with pytest.raises(VendorError):
            yfin.get_insider_transactions("AAPL", curr_date=PAST_DATE)


# ---------------------------------------------------------------------------
# 내부자 거래 — Alpha Vantage
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestAlphaVantageInsiderLookahead:
    _PAYLOAD = json.dumps({
        "data": [
            {"transaction_date": TODAY, "executive": "FUTURE EXEC", "shares": "10"},
            {
                "transaction_date": (
                    date.today() - timedelta(days=400)
                ).strftime("%Y-%m-%d"),
                "executive": "PAST EXEC",
                "shares": "20",
            },
            {"executive": "UNDATED EXEC", "shares": "30"},  # 날짜 없음 -> 제외돼야 함
        ]
    })

    def test_future_and_undated_rows_filtered(self, monkeypatch):
        """미래 거래와 날짜 없는 거래가 모두 걸러지는지 검증하는 테스트."""
        monkeypatch.setattr(avn, "_make_api_request", lambda fn, params: self._PAYLOAD)
        out = avn.get_insider_transactions("IBM", curr_date=PAST_DATE)
        parsed = json.loads(out)
        executives = [r["executive"] for r in parsed["data"]]
        assert executives == ["PAST EXEC"]  # 미래·무일자 항목 0건

    def test_no_curr_date_passes_through(self, monkeypatch):
        """curr_date 미지정(실시간) 시 응답을 그대로 반환하는지 검증하는 테스트."""
        monkeypatch.setattr(avn, "_make_api_request", lambda fn, params: self._PAYLOAD)
        assert avn.get_insider_transactions("IBM") == self._PAYLOAD

    def test_non_json_body_fails_closed(self, monkeypatch):
        """필터를 적용할 수 없는 본문이면 예외를 던지는지 검증하는 테스트."""
        from tradingagents.dataflows.errors import VendorError
        monkeypatch.setattr(avn, "_make_api_request", lambda fn, params: "not-json")
        with pytest.raises(VendorError):
            avn.get_insider_transactions("IBM", curr_date=PAST_DATE)


# ---------------------------------------------------------------------------
# 펀더멘털 스냅샷 경고 배너
# ---------------------------------------------------------------------------
def _fake_info_ticker(info):
    class FakeTicker:
        def __init__(self, symbol):
            pass

        @property
        def info(self):
            return info

    return FakeTicker


@pytest.mark.unit
class TestFundamentalsSnapshotBanner:
    _INFO = {"longName": "Apple Inc.", "marketCap": 3_000_000_000_000}

    def test_yfinance_past_run_has_banner_first(self, monkeypatch):
        """1년 전 curr_date 실행 시 반환 텍스트 맨 앞에 경고 배너가 붙는지 검증하는 테스트."""
        monkeypatch.setattr(yfin.yf, "Ticker", _fake_info_ticker(self._INFO))
        out = yfin.get_fundamentals("AAPL", curr_date=PAST_DATE)
        assert out.startswith(BANNER_MARK)
        assert PAST_DATE in out
        assert "Apple Inc." in out  # 데이터 자체는 유지(경고이지 차단이 아님)

    def test_yfinance_live_run_has_no_banner(self, monkeypatch):
        """오늘 날짜(실시간) 실행에는 배너가 붙지 않는지 검증하는 테스트."""
        monkeypatch.setattr(yfin.yf, "Ticker", _fake_info_ticker(self._INFO))
        out = yfin.get_fundamentals("AAPL", curr_date=TODAY)
        assert BANNER_MARK not in out

    def test_alpha_vantage_past_run_has_banner_first(self, monkeypatch):
        """Alpha Vantage OVERVIEW도 과거 실행 시 동일한 배너가 붙는지 검증하는 테스트."""
        monkeypatch.setattr(
            avf, "_make_api_request", lambda fn, params: '{"Symbol": "AAPL"}'
        )
        out = avf.get_fundamentals("AAPL", curr_date=PAST_DATE)
        assert out.startswith(BANNER_MARK)
        assert PAST_DATE in out

    def test_alpha_vantage_live_run_unchanged(self, monkeypatch):
        """Alpha Vantage OVERVIEW가 실시간 실행에서는 원본 그대로인지 검증하는 테스트."""
        monkeypatch.setattr(
            avf, "_make_api_request", lambda fn, params: '{"Symbol": "AAPL"}'
        )
        assert avf.get_fundamentals("AAPL", curr_date=TODAY) == '{"Symbol": "AAPL"}'


# ---------------------------------------------------------------------------
# StockTwits — 타임스탬프 필터 + 경고 배너
# ---------------------------------------------------------------------------
def _stocktwits_resp(payload):
    class _Resp:
        def __enter__(self_inner):
            return self_inner

        def __exit__(self_inner, *a):
            return False

        def read(self_inner):
            return json.dumps(payload).encode("utf-8")

    return _Resp()


@pytest.mark.unit
class TestStockTwitsLookahead:
    def _payload(self):
        past_created = (date.today() - timedelta(days=400)).strftime(
            "%Y-%m-%dT12:00:00Z"
        )
        return {
            "messages": [
                {
                    "created_at": f"{TODAY}T12:00:00Z",
                    "body": "FUTURE MESSAGE",
                    "user": {"username": "future_user"},
                    "entities": {"sentiment": {"basic": "Bullish"}},
                },
                {
                    "created_at": past_created,
                    "body": "PAST MESSAGE",
                    "user": {"username": "past_user"},
                    "entities": {"sentiment": {"basic": "Bearish"}},
                },
                {
                    # 타임스탬프 없음 -> 과거 실행에서는 제외돼야 함
                    "body": "UNDATED MESSAGE",
                    "user": {"username": "undated_user"},
                    "entities": {},
                },
            ]
        }

    def test_past_run_filters_future_and_adds_banner(self, monkeypatch):
        """1년 전 curr_date 실행 시 미래·무일자 메시지가 0건이고 배너가 붙는지 검증하는 테스트."""
        monkeypatch.setattr(
            stocktwits, "urlopen", lambda req, timeout=None: _stocktwits_resp(self._payload())
        )
        out = stocktwits.fetch_stocktwits_messages("NVDA", curr_date=PAST_DATE)
        assert out.startswith(BANNER_MARK)
        assert PAST_DATE in out
        assert "PAST MESSAGE" in out
        assert "FUTURE MESSAGE" not in out
        assert "UNDATED MESSAGE" not in out

    def test_past_run_all_future_reports_no_messages(self, monkeypatch):
        """모든 메시지가 미래면 배너 + 명확한 '없음' 안내를 반환하는지 검증하는 테스트."""
        payload = {"messages": [{
            "created_at": f"{TODAY}T12:00:00Z", "body": "FUTURE ONLY",
            "user": {"username": "u"}, "entities": {},
        }]}
        monkeypatch.setattr(
            stocktwits, "urlopen", lambda req, timeout=None: _stocktwits_resp(payload)
        )
        out = stocktwits.fetch_stocktwits_messages("NVDA", curr_date=PAST_DATE)
        assert BANNER_MARK in out
        assert "no StockTwits messages" in out
        assert "FUTURE ONLY" not in out

    def test_live_run_keeps_everything_without_banner(self, monkeypatch):
        """curr_date 미지정(실시간) 시 기존 동작이 유지되는지 검증하는 테스트."""
        monkeypatch.setattr(
            stocktwits, "urlopen", lambda req, timeout=None: _stocktwits_resp(self._payload())
        )
        out = stocktwits.fetch_stocktwits_messages("NVDA")
        assert BANNER_MARK not in out
        assert "FUTURE MESSAGE" in out
        assert "UNDATED MESSAGE" in out


# ---------------------------------------------------------------------------
# Reddit — 타임스탬프 필터 + 경고 배너
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestRedditLookahead:
    def _posts(self):
        from datetime import datetime, timezone
        past_epoch = datetime.strptime(PAST_DATE, "%Y-%m-%d").replace(
            tzinfo=timezone.utc
        ).timestamp() - 86400 * 5
        future_epoch = datetime.now(timezone.utc).timestamp()
        return [
            {"title": "PAST POST", "score": 10, "num_comments": 2,
             "created_utc": past_epoch, "selftext": ""},
            {"title": "FUTURE POST", "score": 99, "num_comments": 9,
             "created_utc": future_epoch, "selftext": ""},
            {"title": "UNDATED POST", "score": None, "num_comments": None,
             "created_utc": None, "selftext": "", "source": "rss"},
        ]

    def test_past_run_filters_future_and_adds_banner(self, monkeypatch):
        """1년 전 curr_date 실행 시 미래·무일자 게시글이 0건이고 배너가 붙는지 검증하는 테스트."""
        monkeypatch.setattr(
            reddit, "_fetch_subreddit", lambda t, s, limit, timeout: self._posts()
        )
        out = reddit.fetch_reddit_posts(
            "NVDA", subreddits=("stocks",), inter_request_delay=0,
            curr_date=PAST_DATE,
        )
        assert out.startswith(BANNER_MARK)
        assert PAST_DATE in out
        assert "PAST POST" in out
        assert "FUTURE POST" not in out
        assert "UNDATED POST" not in out

    def test_live_run_keeps_everything_without_banner(self, monkeypatch):
        """curr_date 미지정(실시간) 시 기존 동작이 유지되는지 검증하는 테스트."""
        monkeypatch.setattr(
            reddit, "_fetch_subreddit", lambda t, s, limit, timeout: self._posts()
        )
        out = reddit.fetch_reddit_posts(
            "NVDA", subreddits=("stocks",), inter_request_delay=0,
        )
        assert BANNER_MARK not in out
        assert "FUTURE POST" in out
        assert "UNDATED POST" in out


# ---------------------------------------------------------------------------
# 재무제표 — 공시 지연(+45일) 컷오프
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestFinancialsDisclosureLag:
    def test_yfinance_columns_within_lag_window_removed(self):
        """종료일이 지났어도 공시 전(+45일 이내)인 분기 컬럼이 제거되는지 검증하는 테스트."""
        df = pd.DataFrame(
            {
                pd.Timestamp("2024-06-30"): [1.0],  # 공시 예정일 ~2024-08-14 -> 제거
                pd.Timestamp("2024-03-31"): [2.0],  # 공시 완료(2024-05-15 이전) -> 유지
            },
            index=["Total Assets"],
        )
        out = su.filter_financials_by_date(df, "2024-07-15")
        assert list(out.columns) == [pd.Timestamp("2024-03-31")]

    def test_yfinance_no_curr_date_passes_through(self):
        """curr_date가 없으면(실시간) 필터가 꺼진 기존 동작이 유지되는지 검증하는 테스트."""
        df = pd.DataFrame({pd.Timestamp("2024-06-30"): [1.0]}, index=["Total Assets"])
        out = su.filter_financials_by_date(df, None)
        assert list(out.columns) == [pd.Timestamp("2024-06-30")]

    def test_alpha_vantage_reports_within_lag_window_removed(self):
        """AV 보고서도 종료일 + 45일 공시 지연이 반영되는지 검증하는 테스트."""
        payload = json.dumps({
            "quarterlyReports": [
                {"fiscalDateEnding": "2024-06-30", "totalAssets": "1"},  # 공시 전 -> 제거
                {"fiscalDateEnding": "2024-03-31", "totalAssets": "2"},  # 공시 완료 -> 유지
                {"totalAssets": "3"},  # 날짜 없음 -> 제거(fail-closed)
            ],
        })
        out = avf._filter_reports_by_date(payload, "2024-07-15")
        parsed = json.loads(out)
        assert [r["fiscalDateEnding"] for r in parsed["quarterlyReports"]] == [
            "2024-03-31"
        ]
