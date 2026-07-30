# ============================================================================
# [모듈 개요 - 초보자용]
# 이 파일은 야후 파이낸스(yfinance)에서 특정 종목의 뉴스와 글로벌 거시경제
# 뉴스를 가져오는 모듈입니다. 기사 발행 시각을 UTC 기준으로 통일해 요청한
# 날짜 창(window) 안의 기사만 남기므로, 백테스트에서 미래 뉴스가 새어
# 들어오는 것(look-ahead, 선견 편향)을 막습니다.
# TradingAgents(LLM 멀티 에이전트 주식 트레이딩 프레임워크)에서 뉴스 분석가
# 에이전트의 데이터 소스로 쓰입니다.
# ============================================================================

"""yfinance 기반 뉴스 데이터 수집 함수들."""

import contextlib
from datetime import datetime, timedelta, timezone

import yfinance as yf
from dateutil.relativedelta import relativedelta

from .config import get_config
from .stockstats_utils import yf_retry
from .symbol_utils import normalize_symbol


def _as_utc(dt: datetime) -> datetime:
    """datetime을 UTC 시간대 인식(aware) 값으로 정규화한다; naive 값은 UTC로 간주한다.

    날짜 창(window)의 경계는 ``yyyy-mm-dd``에서 파싱되어 시간대 정보 없이
    (naive) 도착하는 반면, 기사 타임스탬프는 시간대가 붙어(offset-aware)
    있을 수 있으므로 비교 전에 모든 피연산자를 정규화합니다. 이것이 없으면
    필터 결과가 실행 호스트의 시간대에 따라 달라집니다(#1126).
    """
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _extract_article_data(article: dict) -> dict:
    """yfinance 뉴스 형식에서 기사 데이터를 추출한다(중첩된 'content' 구조 처리)."""
    # 중첩된 content 구조 처리
    if "content" in article:
        content = article["content"]
        title = content.get("title", "No title")
        summary = content.get("summary", "")
        provider = content.get("provider", {})
        publisher = provider.get("displayName", "Unknown")

        # canonicalUrl 또는 clickThroughUrl에서 URL을 얻는다
        url_obj = content.get("canonicalUrl") or content.get("clickThroughUrl") or {}
        link = url_obj.get("url", "")

        # 발행 일시를 얻는다
        pub_date_str = content.get("pubDate", "")
        pub_date = None
        if pub_date_str:
            with contextlib.suppress(ValueError, AttributeError):
                pub_date = datetime.fromisoformat(pub_date_str.replace("Z", "+00:00"))

        return {
            "title": title,
            "summary": summary,
            "publisher": publisher,
            "link": link,
            "pub_date": pub_date,
        }
    else:
        # 평평한(flat) 구조를 위한 폴백. 에포크(epoch) 발행 시각을 파싱해서
        # 평평한 구조의 기사도 날짜로 필터링할 수 있게 합니다(그러지 않으면
        # 과거 날짜 창을 우회해 미래 뉴스가 새어 듭니다, #992/#1007).
        pub_date = None
        ts = article.get("providerPublishTime")
        if ts:
            # 에포크 초는 UTC입니다; 필터링이 호스트 시간대에 따라 흔들리지
            # 않도록 UTC 인식 값으로 파싱합니다(#1126).
            with contextlib.suppress(ValueError, OSError, TypeError):
                pub_date = datetime.fromtimestamp(ts, tz=timezone.utc)
        return {
            "title": article.get("title", "No title"),
            "summary": article.get("summary", ""),
            "publisher": article.get("publisher", "Unknown"),
            "link": article.get("link", ""),
            "pub_date": pub_date,
        }


def _in_news_window(pub_date, start_dt, end_dt) -> bool:
    """기사가 반개(half-open) 구간 ``[start, end + 1 day)``에 속하는지 여부.

    모든 피연산자를 UTC로 정규화하고, 상한을 미포함(exclusive)으로 두어
    ``end_dt`` 다음 날 자정 정각에 찍힌 기사가 과거 실행에 새어 들지
    못하게 합니다(#1126). 날짜 없는 기사는 창이 현재에 닿을 때(실시간 실행)
    만 유지합니다 — 과거/백테스트 창에서는 미래 뉴스가 아니라고 증명할 수
    없으므로 제외합니다(#992/#1007).
    """
    end = _as_utc(end_dt)
    if pub_date is not None:
        return _as_utc(start_dt) <= _as_utc(pub_date) < end + timedelta(days=1)
    return end >= datetime.now(timezone.utc) - timedelta(days=1)


def get_news_yfinance(
    ticker: str,
    start_date: str,
    end_date: str,
) -> str:
    """
    yfinance를 사용해 특정 종목 티커의 뉴스를 가져온다.

    Args:
        ticker: 종목 티커 심볼 (예: "AAPL")
        start_date: yyyy-mm-dd 형식의 시작 날짜
        end_date: yyyy-mm-dd 형식의 종료 날짜

    Returns:
        뉴스 기사를 담은 형식화된 문자열
    """
    article_limit = get_config()["news_article_limit"]
    # 다른 모든 yfinance 경로처럼 정식(canonical) 심볼로 야후에 질의합니다 —
    # 브로커/외환/암호화폐 별칭(XAUUSD, BTCUSD)을 그대로 쓰면 조용히
    # 뉴스가 없다고 나옵니다. 보고서 헤더에는 사용자가 입력한 티커를 유지합니다.
    canonical = normalize_symbol(ticker)
    resolved = "" if canonical == ticker else f" (resolved to {canonical})"
    try:
        stock = yf.Ticker(canonical)
        news = yf_retry(lambda: stock.get_news(count=article_limit))

        if not news:
            return f"No news found for {ticker}{resolved}"

        # 필터링을 위한 날짜 범위를 파싱한다
        start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.strptime(end_date, "%Y-%m-%d")

        news_str = ""
        filtered_count = 0

        for article in news:
            data = _extract_article_data(article)

            # 요청한 날짜 창 안의 기사만 유지한다 (선견 편향 안전).
            if not _in_news_window(data["pub_date"], start_dt, end_dt):
                continue

            news_str += f"### {data['title']} (source: {data['publisher']})\n"
            if data["summary"]:
                news_str += f"{data['summary']}\n"
            if data["link"]:
                news_str += f"Link: {data['link']}\n"
            news_str += "\n"
            filtered_count += 1

        if filtered_count == 0:
            return f"No news found for {ticker}{resolved} between {start_date} and {end_date}"

        return f"## {ticker}{resolved} News, from {start_date} to {end_date}:\n\n{news_str}"

    except Exception as e:
        return f"Error fetching news for {ticker}: {str(e)}"


def get_global_news_yfinance(
    curr_date: str,
    look_back_days: int | None = None,
    limit: int | None = None,
) -> str:
    """
    yfinance Search를 사용해 글로벌/거시경제 뉴스를 가져온다.

    Args:
        curr_date: yyyy-mm-dd 형식의 현재 날짜
        look_back_days: 되돌아볼 일수. ``None``이면 활성 설정의
            ``global_news_lookback_days``로 폴백.
        limit: 반환할 최대 기사 수. ``None``이면 활성 설정의
            ``global_news_article_limit``로 폴백.

    Returns:
        글로벌 뉴스 기사를 담은 형식화된 문자열
    """
    config = get_config()
    if look_back_days is None:
        look_back_days = config["global_news_lookback_days"]
    if limit is None:
        limit = config["global_news_article_limit"]
    search_queries = config["global_news_queries"]

    all_news = []
    seen_titles = set()

    try:
        for query in search_queries:
            search = yf_retry(lambda q=query: yf.Search(
                query=q,
                news_count=limit,
                enable_fuzzy_query=True,
            ))

            if search.news:
                for article in search.news:
                    # 평평한 구조와 중첩 구조 모두 처리한다
                    if "content" in article:
                        data = _extract_article_data(article)
                        title = data["title"]
                    else:
                        title = article.get("title", "")

                    # 제목으로 중복 제거
                    if title and title not in seen_titles:
                        seen_titles.add(title)
                        all_news.append(article)

            if len(all_news) >= limit:
                break

        if not all_news:
            return f"No global news found for {curr_date}"

        # 날짜 범위를 계산한다
        curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
        start_dt = curr_dt - relativedelta(days=look_back_days)
        start_date = start_dt.strftime("%Y-%m-%d")

        news_str = ""
        kept = 0
        for article in all_news[:limit]:
            # (평평한 구조든 중첩 구조든) 동일하게 추출하고 동일한 선견 편향
            # 안전 창 필터를 적용해, 평평한 구조의 기사도 미래 뉴스를 흘리지
            # 못하게 합니다(#1007).
            data = _extract_article_data(article)
            if not _in_news_window(data["pub_date"], start_dt, curr_dt):
                continue
            news_str += f"### {data['title']} (source: {data['publisher']})\n"
            if data["summary"]:
                news_str += f"{data['summary']}\n"
            if data["link"]:
                news_str += f"Link: {data['link']}\n"
            news_str += "\n"
            kept += 1

        # 후보 기사가 모두 날짜 창 밖으로 떨어졌다면 -> 본문이 빈 보고서를
        # 돌려주는 대신 그렇다고 말한다(#993).
        if kept == 0:
            return f"No global news found between {start_date} and {curr_date}"

        return f"## Global Market News, from {start_date} to {curr_date}:\n\n{news_str}"

    except Exception as e:
        return f"Error fetching global news: {str(e)}"
