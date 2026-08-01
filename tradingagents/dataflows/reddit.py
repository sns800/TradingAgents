# ============================================================================
# [모듈 개요 - 초보자용]
# 이 파일은 레딧(Reddit)의 금융 커뮤니티(서브레딧)에서 특정 종목을 언급한
# 최근 게시글을 검색해 오는 모듈입니다. API 키 없이 공개 RSS 검색 피드를
# 사용하며, 요청 제한(rate limit)에 걸리면 잠시 기다렸다가 한 번 재시도합니다.
# TradingAgents(LLM 멀티 에이전트 주식 트레이딩 프레임워크)에서 소셜 미디어
# 분석가 에이전트가 개인 투자자들의 여론을 파악하는 데이터 소스로 쓰입니다.
# ============================================================================

"""티커별 토론 게시글을 가져오는 Reddit 검색 수집기.

기본 경로는 Reddit의 공개 Atom/RSS 검색 피드
(``reddit.com/r/{sub}/search.rss``)입니다. 더 풍부한 JSON 검색 엔드포인트
(``/search.json``)는 공개 클라이언트에 대해 WAF(웹 방화벽)가 확실하게
차단하고(``HTTP 403``, 이슈 #862), 매 호출마다 그것을 먼저 시도하는 것은
Reddit의 IP당 요청 제한에 대한 요청량만 두 배로 늘려 RSS 폴백에서
``429``를 유발했기에, 코드는 남겨두되(``_fetch_subreddit_json``) 기본으로는
쓰지 않습니다. 429를 받으면 (``Retry-After`` 헤더를 존중하며) 한 번만
백오프(backoff, 대기 후 재시도)합니다. RSS에는 점수/댓글 수가 없으므로,
그런 게시글은 표시를 남기고 포매터가 가짜 0을 찍는 대신 지표를 생략합니다.

API 키가 필요 없습니다. 프롬프트에 바로 넣을 수 있는 형식의 일반 텍스트
블록을 반환하고, 우아하게 완화됩니다 — 예외를 던지는 대신 자리표시
문자열을 반환하므로 호출자는 누락 데이터를 따로 처리할 일이 없습니다.
"""

from __future__ import annotations

import html
import http.client
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .symbol_utils import crypto_base
from .utils import is_historical_run, snapshot_warning_banner

logger = logging.getLogger(__name__)

_API = "https://www.reddit.com/r/{sub}/search.json?{qs}"
_RSS = "https://www.reddit.com/r/{sub}/search.rss?{qs}"
# 서술적이고 신원이 드러나는 User-Agent(Reddit API 에티켓 준수). Reddit은
# 맨 "Mozilla/5.0"이나 "curl/…" 같은 일반적/익명 토큰은 차단하지만 이
# 값은 두 엔드포인트 모두에서 통과시킵니다; JSON 검색 엔드포인트가 403을
# 내는 상황에서도 RSS 피드는 이 값을 받아 주므로 브라우저 위장이 필요 없습니다.
_UA = "tradingagents/0.2 (+https://github.com/TauricResearch/TradingAgents)"
_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}

# 티커별 토론의 신호 밀도(잡음 대비 유용한 정보량) 순으로 대략 정렬한 기본
# 서브레딧 목록. wallstreetbets는 글이 가장 많지만 잡음도 가장 많고,
# stocks/investing은 더 신중한 경향이 있습니다. 호출자가 재정의할 수 있습니다.
DEFAULT_SUBREDDITS = ("wallstreetbets", "stocks", "investing")


def _search_qs(ticker: str, limit: int) -> str:
    # Reddit 검색용 쿼리 스트링을 만든다.
    return urlencode({
        "q": ticker,
        "restrict_sr": "on",
        "sort": "new",
        "t": "week",  # 최근 7일
        "limit": limit,
    })


def _iso_to_timestamp(iso_str: str | None) -> float | None:
    """Atom의 ``published`` 타임스탬프를 UTC 에포크(epoch) 초로 파싱한다. 실패 시 None."""
    if not iso_str:
        return None
    try:
        normalized = iso_str[:-1] + "+00:00" if iso_str.endswith("Z") else iso_str
        return datetime.fromisoformat(normalized).timestamp()
    except (ValueError, TypeError):
        return None


def _strip_html(content: str) -> str:
    """Reddit이 Atom 엔트리에 담아 보내는 HTML 본문을 일반 텍스트로 정리한다."""
    if not content:
        return ""
    # Reddit은 실제 본문(selftext)을 SC_OFF / SC_ON 마커 사이에 감싸서 보낸다.
    if "<!-- SC_OFF -->" in content and "<!-- SC_ON -->" in content:
        content = content.split("<!-- SC_OFF -->")[1].split("<!-- SC_ON -->")[0]
    text = re.sub(r"<[^>]+>", " ", content)
    return " ".join(html.unescape(text).split())


def _retry_after_seconds(exc: HTTPError) -> float | None:
    """429 응답의 ``Retry-After`` 헤더에서 대기 시간(초)을 얻는다. 최대 30초로 제한."""
    try:
        val = exc.headers.get("Retry-After") if getattr(exc, "headers", None) else None
        return min(float(val), 30.0) if val else None
    except (ValueError, TypeError, AttributeError):
        return None


def _fetch_subreddit_rss(
    ticker: str,
    sub: str,
    limit: int,
    timeout: float,
    _retry: bool = True,
) -> list[dict]:
    """기본 경로: 서브레딧의 공개 Atom 검색 피드를 파싱한다.

    점수/댓글 수가 실려 오지 않으므로 해당 필드는 None으로 두고, 정직한
    표시를 위해 게시글에 ``source="rss"`` 태그를 답니다. 429(Reddit의
    IP당 요청 제한)를 받으면 — ``Retry-After``가 있으면 존중하며 — 포기하기
    전에 한 번 백오프하므로, 일시적 폭주가 피드를 통째로 비우지 않습니다.
    """
    url = _RSS.format(sub=sub, qs=_search_qs(ticker, limit))
    req = Request(url, headers={"User-Agent": _UA})
    try:
        with urlopen(req, timeout=timeout) as resp:
            root = ET.fromstring(resp.read())
    except HTTPError as exc:
        if exc.code == 429 and _retry:
            wait = _retry_after_seconds(exc) or 5.0
            logger.warning(
                "Reddit RSS 429 for r/%s · %s — backing off %.1fs then retrying once",
                sub, ticker, wait,
            )
            time.sleep(wait)
            return _fetch_subreddit_rss(ticker, sub, limit, timeout, _retry=False)
        logger.warning("Reddit RSS fetch failed for r/%s · %s: %s", sub, ticker, exc)
        return []
    except (OSError, http.client.HTTPException, ET.ParseError) as exc:
        # OSError는 URLError/TimeoutError/연결 리셋을 포괄하고, HTTPException은
        # 청크 전송(chunked-transfer) 오류(IncompleteRead/BadStatusLine, #1024)를 포괄합니다.
        logger.warning("Reddit RSS fetch failed for r/%s · %s: %s", sub, ticker, exc)
        return []

    posts = []
    for entry in root.findall("atom:entry", _ATOM_NS)[:limit]:
        title_el = entry.find("atom:title", _ATOM_NS)
        published_el = entry.find("atom:published", _ATOM_NS)
        content_el = entry.find("atom:content", _ATOM_NS)
        posts.append({
            "title": (title_el.text if title_el is not None else "") or "",
            "score": None,
            "num_comments": None,
            "created_utc": _iso_to_timestamp(
                published_el.text if published_el is not None else None
            ),
            "selftext": _strip_html(content_el.text if content_el is not None else ""),
            "source": "rss",
        })
    return posts


def _fetch_subreddit_json(
    ticker: str,
    sub: str,
    limit: int,
    timeout: float,
) -> list[dict]:
    """더 풍부한 JSON 검색 경로(점수/댓글 수가 실려 온다).

    Reddit의 WAF가 현재 비(非)OAuth 클라이언트의 이 엔드포인트 요청에
    ``403 Blocked``를 반환하므로(이슈 #862), 기본으로는 쓰지 않습니다 —
    매 요청마다 호출하면 IP당 요청 제한 대비 요청량만 두 배가 되어 RSS
    폴백에서 429를 유발했습니다. WAF가 완화되거나 OAuth 토큰이 연결될 날을
    위해 남겨 둡니다; 실패 시 RSS로 완화됩니다.
    """
    url = _API.format(sub=sub, qs=_search_qs(ticker, limit))
    req = Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
        children = (payload.get("data") or {}).get("children") or []
        return [c.get("data", {}) for c in children if isinstance(c, dict)]
    except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
        logger.warning(
            "Reddit JSON fetch failed for r/%s · %s: %s — falling back to RSS feed.",
            sub, ticker, exc,
        )
        return _fetch_subreddit_rss(ticker, sub, limit, timeout)


def _fetch_subreddit(
    ticker: str,
    sub: str,
    limit: int,
    timeout: float,
) -> list[dict]:
    """서브레딧 하나를 RSS 우선으로 가져온다.

    JSON 검색 엔드포인트는 공개 클라이언트에 대해 WAF가 확실하게
    차단하므로(403), 신원이 드러나는 User-Agent를 안정적으로 받아 주는
    RSS 피드로 바로 갑니다 — Reddit의 IP당 요청 제한 대비 요청량이
    절반으로 줄어듭니다.
    """
    return _fetch_subreddit_rss(ticker, sub, limit, timeout)


def _filter_posts_by_date(
    posts: list[dict], curr_date: str | None, historical: bool
) -> list[dict]:
    """curr_date 이후에 작성된 게시글을 제거해 룩어헤드(look-ahead)를 방지한다.

    ``created_utc`` (UTC 에포크 초)가 curr_date 당일 자정(다음 날, 미포함)
    이전인 게시글만 남깁니다. 타임스탬프가 없는 게시글은 과거 날짜 실행에서
    미래가 아니라고 증명할 수 없으므로 제외하고, 실시간 실행에서는 유지합니다.
    """
    if not curr_date:
        return posts
    try:
        cutoff_epoch = (
            datetime.strptime(curr_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            + timedelta(days=1)  # curr_date 당일 전체 포함(다음 날 자정 미포함)
        ).timestamp()
    except (ValueError, TypeError):
        return posts
    def _visible(p: dict) -> bool:
        created = p.get("created_utc")
        if created is None:
            # 타임스탬프 없는 게시글: 과거 실행에서는 미래가 아니라고 증명할
            # 수 없어 제외하고, 실시간 실행에서는 유지한다.
            return not historical
        return created < cutoff_epoch

    return [p for p in posts if _visible(p)]


def fetch_reddit_posts(
    ticker: str,
    subreddits: Iterable[str] = DEFAULT_SUBREDDITS,
    limit_per_sub: int = 5,
    timeout: float = 10.0,
    inter_request_delay: float = 1.0,
    curr_date: str | None = None,
) -> str:
    """금융 서브레딧들에서 ``ticker``를 언급한 최근 Reddit 게시글을 가져와
    형식을 갖춘 일반 텍스트 블록으로 반환한다.

    ``inter_request_delay``는 (이제 RSS 전용인) 서브레딧별 요청 사이에
    간격을 두어 Reddit의 공개 IP당 요청 제한을 넘지 않게 합니다; RSS 우선
    경로와 결합하면 여러 분석이 연달아 돌아도 429가 드물어집니다.

    ``curr_date`` (yyyy-mm-dd)가 주어지면 그 날짜 이후에 작성된 게시글
    (``created_utc`` 기준)을 걸러내 백테스트의 룩어헤드를 막고, 과거 날짜
    실행이면 결과 앞에 스냅샷 경고 배너를 붙입니다 — Reddit 검색은 '지금'
    기준 최근 글만 반환하므로 과거 시점 여론을 되돌려 볼 수 없기 때문입니다.
    """
    # 암호화폐는 야후 페어(BTC-USD) 형태로 들어오는데, Reddit에서는 기초
    # 자산("BTC")으로 검색해야 거의 아무것도 안 걸리는 대신 실제 토론이 걸립니다.
    ticker = crypto_base(ticker) or ticker
    historical = is_historical_run(curr_date)
    banner = snapshot_warning_banner(curr_date, "Reddit 감성") if historical else ""
    blocks = []
    total_posts = 0
    for i, sub in enumerate(subreddits):
        if i > 0:
            time.sleep(inter_request_delay)
        posts = _fetch_subreddit(ticker, sub, limit_per_sub, timeout)
        posts = _filter_posts_by_date(posts, curr_date, historical)
        total_posts += len(posts)
        if not posts:
            blocks.append(f"r/{sub}: <no posts found mentioning {ticker.upper()} in the past 7 days>")
            continue

        via_rss = any(p.get("source") == "rss" for p in posts)
        header = f"r/{sub} — {len(posts)} recent posts mentioning {ticker.upper()}"
        header += " (via RSS feed; scores/comments unavailable):" if via_rss else ":"
        lines = [header]
        for p in posts:
            title = (p.get("title") or "").replace("\n", " ").strip()
            score = p.get("score")
            comments = p.get("num_comments")
            created = p.get("created_utc")
            created_str = (
                time.strftime("%Y-%m-%d", time.gmtime(created)) if created else "?"
            )
            # RSS 폴백 경로에서는 점수/댓글 수가 없습니다 — 가짜 0을 찍는
            # 대신 값이 있을 때만 표시합니다.
            meta = created_str
            if score is not None and comments is not None:
                meta += f" · {score:>4}↑ · {comments:>3}c"
            selftext = (p.get("selftext") or "").replace("\n", " ").strip()
            if len(selftext) > 240:
                selftext = selftext[:240] + "…"
            lines.append(
                f"  [{meta}] {title}"
                + (f"\n    body excerpt: {selftext}" if selftext else "")
            )
        blocks.append("\n".join(lines))

    if total_posts == 0:
        return banner + (
            f"<no Reddit posts found mentioning {ticker.upper()} across "
            f"{', '.join(f'r/{s}' for s in subreddits)} in the past 7 days>"
        )
    return banner + "\n\n".join(blocks)
