"""[모듈 개요] RSS 우선(RSS-first) 레딧(Reddit) 수집기를 검증하는 테스트.

429 응답에 대한 백오프(backoff), 선택적(opt-in) JSON 경로의 성능 저하 대응(#862),
청크 전송(chunked-transfer) 오류 처리(#1024)를 다룬다.
"""

from __future__ import annotations

import http.client
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from tradingagents.dataflows import reddit

_SAMPLE_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>NVDA earnings beat, stock pops</title>
    <published>2026-05-20T14:30:00+00:00</published>
    <content type="html">&lt;!-- SC_OFF --&gt;&lt;div class="md"&gt;&lt;p&gt;Great &lt;b&gt;quarter&lt;/b&gt; for NVDA&amp;#39;s datacenter unit.&lt;/p&gt;&lt;/div&gt;&lt;!-- SC_ON --&gt;</content>
  </entry>
  <entry>
    <title>Is NVDA overvalued?</title>
    <published>2026-05-19T09:00:00Z</published>
    <content type="html">&lt;p&gt;Forward P/E discussion&lt;/p&gt;</content>
  </entry>
</feed>
"""


def _resp(read_fn):
    """read()가 ``read_fn``을 실행하는 최소한의 컨텍스트 매니저(context manager) 응답 객체."""
    class _Resp:
        def __enter__(self_inner):
            return self_inner

        def __exit__(self_inner, *a):
            return False

        def read(self_inner):
            return read_fn()
    return _Resp()


def _atom_resp():
    return _resp(lambda: _SAMPLE_ATOM.encode("utf-8"))


def _raise(exc):
    def _r():
        raise exc
    return _resp(_r)


@pytest.mark.unit
class TestIsoToTimestamp:
    def test_parses_offset_and_z(self):
        """오프셋 형식과 Z 접미사 형식의 ISO 시각이 모두 파싱되는지 검증하는 테스트."""
        assert reddit._iso_to_timestamp("2026-05-20T14:30:00+00:00") > 0
        assert reddit._iso_to_timestamp("2026-05-19T09:00:00Z") > 0

    def test_none_and_garbage_return_none(self):
        """None이나 잘못된 문자열 입력 시 None을 반환하는지 검증하는 테스트."""
        assert reddit._iso_to_timestamp(None) is None
        assert reddit._iso_to_timestamp("not-a-date") is None


@pytest.mark.unit
class TestStripHtml:
    def test_extracts_between_sc_markers_and_unescapes(self):
        """SC 마커 사이의 본문을 추출하고 HTML 이스케이프를 해제하는지 검증하는 테스트."""
        raw = "<!-- SC_OFF --><div class=\"md\"><p>Great <b>quarter</b> &amp; more</p></div><!-- SC_ON -->"
        assert reddit._strip_html(raw) == "Great quarter & more"

    def test_empty(self):
        """빈 문자열 입력을 그대로 빈 문자열로 처리하는지 검증하는 테스트."""
        assert reddit._strip_html("") == ""


@pytest.mark.unit
class TestRssParsing:
    def test_parses_atom_entries(self):
        """Atom 피드의 항목(entry)들이 게시물 목록으로 올바르게 파싱되는지 검증하는 테스트."""
        with patch.object(reddit, "urlopen", return_value=_atom_resp()):
            posts = reddit._fetch_subreddit_rss("NVDA", "stocks", limit=5, timeout=5.0)
        assert len(posts) == 2
        assert posts[0]["title"] == "NVDA earnings beat, stock pops"
        assert posts[0]["source"] == "rss"
        assert posts[0]["score"] is None
        assert posts[0]["num_comments"] is None
        assert posts[0]["created_utc"] > 0
        assert "datacenter unit" in posts[0]["selftext"]

    def test_malformed_xml_fails_open(self):
        """잘못된 XML을 받아도 예외 없이 빈 목록을 반환하는지 검증하는 테스트."""
        with patch.object(reddit, "urlopen", return_value=_resp(lambda: b"<<not xml>>")):
            assert reddit._fetch_subreddit_rss("NVDA", "stocks", 5, 5.0) == []


@pytest.mark.unit
class TestFetchSubredditIsRssFirst:
    """기본 서브레딧(subreddit) 수집이 RSS로 직행하는지 검증하는 테스트 모음.

    WAF에 차단된 JSON 엔드포인트는 요청 한도(rate-limit) 예산만 낭비하므로
    절대 호출하면 안 된다.
    """

    def test_delegates_to_rss_without_touching_json(self):
        """JSON 엔드포인트를 건드리지 않고 RSS 수집 함수에 위임하는지 검증하는 테스트."""
        sentinel = [{"title": "x", "source": "rss", "score": None,
                     "num_comments": None, "created_utc": None, "selftext": ""}]
        with patch.object(reddit, "_fetch_subreddit_rss", return_value=sentinel) as rss, \
             patch.object(reddit, "urlopen",
                          side_effect=AssertionError("JSON endpoint must not be called")):
            out = reddit._fetch_subreddit("NVDA", "stocks", 5, 5.0)
        rss.assert_called_once()
        assert out is sentinel


@pytest.mark.unit
class TestJsonPathFallsBackToRss:
    """선택적(opt-in) JSON 경로가 403 응답 시 RSS로 폴백하는지 검증하는 테스트 모음 (#862 유지)."""

    def test_403_triggers_rss(self):
        """JSON 요청이 403으로 차단되면 RSS 수집으로 전환하는지 검증하는 테스트."""
        err = HTTPError("url", 403, "Blocked", {}, None)
        rss_posts = [{"title": "x", "source": "rss", "score": None,
                      "num_comments": None, "created_utc": None, "selftext": ""}]
        with patch.object(reddit, "urlopen", side_effect=err), \
             patch.object(reddit, "_fetch_subreddit_rss", return_value=rss_posts) as rss:
            out = reddit._fetch_subreddit_json("NVDA", "stocks", 5, 5.0)
        rss.assert_called_once()
        assert out and out[0]["source"] == "rss"


@pytest.mark.unit
class TestRss429Backoff:
    def test_429_then_success_retries_once(self):
        """429 후 성공하는 경우 정확히 한 번만 재시도하는지 검증하는 테스트."""
        err = HTTPError("url", 429, "Too Many Requests", {}, None)
        with patch.object(reddit, "urlopen", side_effect=[err, _atom_resp()]) as op, \
             patch.object(reddit.time, "sleep") as slept:
            posts = reddit._fetch_subreddit_rss("NVDA", "stocks", 5, 5.0)
        assert op.call_count == 2          # 원 요청 + 정확히 한 번의 재시도
        slept.assert_called_once()         # 재시도 전에 백오프(backoff)함
        assert len(posts) == 2

    def test_429_twice_gives_up_after_one_retry(self):
        """429가 연속 두 번이면 한 번의 재시도 후 깔끔하게 포기하는지 검증하는 테스트."""
        err = HTTPError("url", 429, "Too Many Requests", {}, None)
        with patch.object(reddit, "urlopen", side_effect=[err, err]) as op, \
             patch.object(reddit.time, "sleep"):
            posts = reddit._fetch_subreddit_rss("NVDA", "stocks", 5, 5.0)
        assert op.call_count == 2          # 한 번 재시도 후 깔끔하게 포기
        assert posts == []

    def test_retry_after_header_is_honoured(self):
        """Retry-After 헤더에 지정된 대기 시간을 그대로 따르는지 검증하는 테스트."""
        err = HTTPError("url", 429, "Too Many Requests", {"Retry-After": "12"}, None)
        with patch.object(reddit, "urlopen", side_effect=[err, _atom_resp()]), \
             patch.object(reddit.time, "sleep") as slept:
            reddit._fetch_subreddit_rss("NVDA", "stocks", 5, 5.0)
        slept.assert_called_once_with(12.0)


@pytest.mark.unit
class TestChunkedTransferErrorsHandled:
    """청크 전송(chunked-transfer) 오류가 처리되는지 검증하는 테스트 모음.

    IncompleteRead/RemoteDisconnected는 http.client에서 나오며 OSError가 아니라서
    예전에는 잡히지 않아 파이프라인 전체를 중단시켰다 (#1024).
    """

    def test_rss_incomplete_read_degrades_to_empty(self):
        """RSS 읽기 중 IncompleteRead가 발생하면 빈 목록으로 대응하는지 검증하는 테스트."""
        with patch.object(reddit, "urlopen", return_value=_raise(http.client.IncompleteRead(b""))):
            assert reddit._fetch_subreddit_rss("NVDA", "stocks", 5, 5.0) == []

    def test_json_incomplete_read_falls_back_to_rss(self):
        """JSON 경로에서 IncompleteRead가 발생하면 RSS로 폴백하는지 검증하는 테스트."""
        with patch.object(reddit, "urlopen", return_value=_raise(http.client.IncompleteRead(b""))), \
             patch.object(reddit, "_fetch_subreddit_rss", return_value=[]) as rss:
            reddit._fetch_subreddit_json("NVDA", "stocks", 5, 5.0)
        rss.assert_called_once()


@pytest.mark.unit
class TestFormatterHandlesRssPosts:
    def test_rss_posts_omit_fake_counts_and_note_source(self):
        """RSS 게시물은 가짜 점수를 표시하지 않고 출처(RSS)를 표기하는지 검증하는 테스트."""
        rss_posts = [{
            "title": "NVDA pops", "score": None, "num_comments": None,
            "created_utc": reddit._iso_to_timestamp("2026-05-20T14:30:00Z"),
            "selftext": "great quarter", "source": "rss",
        }]
        with patch.object(reddit, "_fetch_subreddit", return_value=rss_posts):
            out = reddit.fetch_reddit_posts("NVDA", subreddits=("stocks",), inter_request_delay=0)
        assert "via RSS feed" in out
        assert "↑" not in out  # 가짜 점수 화살표가 없어야 함
        assert "NVDA pops" in out
        assert "great quarter" in out

    def test_json_posts_still_show_counts(self):
        """JSON 게시물은 점수와 댓글 수를 그대로 표시하는지 검증하는 테스트."""
        json_posts = [{
            "title": "NVDA pops", "score": 1234, "num_comments": 56,
            "created_utc": reddit._iso_to_timestamp("2026-05-20T14:30:00Z"),
            "selftext": "",
        }]
        with patch.object(reddit, "_fetch_subreddit", return_value=json_posts):
            out = reddit.fetch_reddit_posts("NVDA", subreddits=("stocks",), inter_request_delay=0)
        assert "1234↑" in out
        assert "56c" in out
        assert "via RSS" not in out


@pytest.mark.unit
class TestCryptoSearchTerm:
    """암호화폐 페어(BTC-USD)는 레딧 본문과 거의 매칭되지 않으므로
    기초 심볼(base symbol)로 검색하는지 검증하는 테스트 모음 (#1113)."""

    def _captured_ticker(self, ticker):
        seen = {}

        def fake_fetch(t, sub, limit, timeout):
            seen["ticker"] = t
            return []

        with patch.object(reddit, "_fetch_subreddit", side_effect=fake_fetch):
            reddit.fetch_reddit_posts(ticker, subreddits=("stocks",), inter_request_delay=0)
        return seen["ticker"]

    def test_crypto_pair_searches_base(self):
        """암호화폐 페어 티커가 기초 심볼(BTC)로 변환되어 검색되는지 검증하는 테스트."""
        assert self._captured_ticker("BTC-USD") == "BTC"

    def test_equity_passes_through(self):
        """일반 주식 티커는 변환 없이 그대로 검색되는지 검증하는 테스트."""
        assert self._captured_ticker("NVDA") == "NVDA"
