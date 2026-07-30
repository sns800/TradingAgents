"""[모듈 개요] yfinance 뉴스가 과거 구간(historical window)에 미래 날짜 기사(또는
백테스트에서 날짜 없는 기사)를 흘려보내지 않는지 검증하는 테스트.

회귀(regression) 방지 대상: #992 (평평한(flat) 구조의 기사가 날짜 필터를 우회),
#1007 (글로벌 뉴스에 미래 기사가 주입됨), #993 (필터 후 결과가 비면 빈 본문을
반환함), #1126 (상한 경계가 포함(inclusive)이라 종료일 다음 자정 기사가 유출되고,
호스트 로컬 시간대로 타임스탬프를 파싱해 필터링이 머신 의존적이었음).
"""
from datetime import datetime, timezone

import pytest

import tradingagents.dataflows.yfinance_news as ynews


def _epoch(date_str):
    """``date_str``의 UTC 자정을 에포크 초(epoch seconds)로 변환한다 (호스트 시간대와 무관)."""
    return int(datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


@pytest.mark.unit
def test_flat_article_publish_time_is_parsed():
    """평평한(flat) 구조의 기사에서도 발행 시각(pub_date)이 제대로 파싱되는지 검증하는 테스트."""
    # #992: flat 기사도 이제 pub_date를 가진다 (예전엔 항상 None -> 필터링 불가).
    # #1126: UTC 인지(aware) 형태로 파싱되므로 호스트 시간대에 따라 날짜가 밀리지 않는다.
    data = ynews._extract_article_data(
        {"title": "X", "publisher": "P", "link": "l", "providerPublishTime": _epoch("2025-05-09")}
    )
    assert data["pub_date"] is not None
    assert data["pub_date"].tzinfo is not None
    assert data["pub_date"] == datetime(2025, 5, 9, tzinfo=timezone.utc)


@pytest.mark.unit
def test_window_excludes_future_and_undated_in_backtest():
    """백테스트(backtest)의 과거 구간에서 미래 기사와 날짜 없는 기사가 제외되는지 검증하는 테스트."""
    start = datetime(2025, 5, 1)
    end = datetime(2025, 5, 9)  # 과거 구간 (충분히 오래된 시점)
    inside = datetime(2025, 5, 5)
    future = datetime(2025, 6, 1)
    assert ynews._in_news_window(inside, start, end) is True
    assert ynews._in_news_window(future, start, end) is False     # 미래 참조(look-ahead) 차단
    assert ynews._in_news_window(None, start, end) is False        # 날짜 없음 -> 백테스트에서는 제외


@pytest.mark.unit
def test_window_keeps_undated_in_live_window():
    """실시간(live) 구간에서는 날짜 없는 기사를 유지하는지 검증하는 테스트."""
    # 실시간 구간(오늘까지 포함): 날짜 없는 기사는 "미래"일 수 없으므로 유지한다.
    now = datetime.now(timezone.utc)
    assert ynews._in_news_window(None, now, now) is True


@pytest.mark.unit
def test_upper_bound_is_exclusive():
    """뉴스 구간의 상한 경계가 배타적(exclusive)으로 처리되는지 검증하는 테스트."""
    # #1126: 예전의 포함(inclusive) 경계에서는 end_date 다음 자정에 찍힌 기사가
    # 유출됐다; 단, end_date 당일 전체는 여전히 유지돼야 한다.
    start = datetime(2025, 5, 1)
    end = datetime(2025, 5, 9)
    midnight_after = datetime(2025, 5, 10, 0, 0, 0, tzinfo=timezone.utc)
    last_moment = datetime(2025, 5, 9, 23, 59, 59, tzinfo=timezone.utc)
    assert ynews._in_news_window(midnight_after, start, end) is False
    assert ynews._in_news_window(last_moment, start, end) is True


@pytest.mark.unit
def test_offset_aware_timestamp_is_converted_not_truncated():
    """시간대 오프셋이 있는 타임스탬프가 잘리지 않고 UTC로 변환되는지 검증하는 테스트."""
    # #1126: 2025-05-10T01:00+05:00은 실제로 2025-05-09T20:00Z -> 구간 안에 있다.
    # tzinfo를 제거하던 예전 동작은 이를 05-10로 오독해 버렸다(제외 처리).
    start = datetime(2025, 5, 1)
    end = datetime(2025, 5, 9)
    aware = datetime.fromisoformat("2025-05-10T01:00:00+05:00")
    assert ynews._in_news_window(aware, start, end) is True


@pytest.mark.unit
def test_global_news_future_flat_article_excluded(monkeypatch):
    """글로벌 뉴스에서 미래 날짜의 flat 기사가 과거 실행 결과에 나타나지 않는지 검증하는 테스트."""
    # #1007: flat 구조의 미래 날짜 글로벌 기사는 과거 시점 실행에 나타나면 안 된다.
    future_article = {"title": "FUTURE EVENT", "publisher": "P", "link": "l",
                      "providerPublishTime": _epoch("2025-06-01")}
    past_article = {"title": "PAST EVENT", "publisher": "P", "link": "l",
                    "providerPublishTime": _epoch("2025-05-05")}

    class FakeSearch:
        def __init__(self, *a, **k):
            self.news = [future_article, past_article]

    monkeypatch.setattr(ynews.yf, "Search", FakeSearch)
    out = ynews.get_global_news_yfinance("2025-05-09", look_back_days=7, limit=10)
    assert "PAST EVENT" in out
    assert "FUTURE EVENT" not in out  # #1007


@pytest.mark.unit
def test_global_news_empty_after_filter_is_informative(monkeypatch):
    """필터링 후 기사가 하나도 없을 때 명확한 안내 메시지를 반환하는지 검증하는 테스트."""
    # #993: 전부 필터링됨 -> 빈 본문 보고서가 아니라 명확한 메시지를 반환해야 한다.
    only_future = {"title": "FUTURE", "publisher": "P", "link": "l",
                   "providerPublishTime": _epoch("2025-06-01")}

    class FakeSearch:
        def __init__(self, *a, **k):
            self.news = [only_future]

    monkeypatch.setattr(ynews.yf, "Search", FakeSearch)
    out = ynews.get_global_news_yfinance("2025-05-09", look_back_days=7, limit=10)
    assert "No global news found" in out
    assert "###" not in out  # 빈 기사 본문이 없어야 함
