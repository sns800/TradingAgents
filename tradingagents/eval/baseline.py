# =============================================================================
# [모듈 개요 — 초보자용]
# 이 파일은 "단일 LLM 베이스라인"을 구현합니다. 멀티 에이전트 파이프라인
# (분석가 4명 → 토론 → 트레이더 → 리스크 토론 → PM) 대신, 같은 날짜·티커의
# 간결한 컨텍스트(주가 요약 + 뉴스 헤드라인)를 단일 LLM에게 한 번만 주고
# 5단계 등급을 받아냅니다. 백테스트에서 이 베이스라인과 전체 파이프라인의
# 성과를 비교하면 "멀티 에이전트 토론이 단일 LLM보다 낫다"는 설계 가정을
# 데이터로 검증할 수 있습니다.
#
# 입력 데이터는 기존 dataflows 인터페이스(route_to_vendor)로 수집하므로,
# 전체 파이프라인과 같은 벤더 라우팅·curr_date 필터(룩어헤드 방지)를 그대로
# 따릅니다. 출력은 "Rating: X" 형식을 강제한 뒤 공용 parse_rating으로
# 추출합니다.
# =============================================================================

"""단일 LLM 1회 호출 베이스라인 — 멀티 에이전트 파이프라인의 비교 대조군."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from tradingagents.agents.utils.rating import RATINGS_5_TIER, parse_rating
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.interface import route_to_vendor
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.llm_clients import create_llm_client

logger = logging.getLogger(__name__)

# 주가 요약에 사용할 되돌아보기 기간(달력일)
PRICE_LOOKBACK_DAYS = 30
# 뉴스 헤드라인에 사용할 되돌아보기 기간(달력일)
NEWS_LOOKBACK_DAYS = 7
# 컨텍스트 섹션당 최대 문자 수 — 단일 호출을 저렴하게 유지하기 위한 상한
MAX_SECTION_CHARS = 4000

# 단일 LLM 베이스라인의 시스템 프롬프트. LLM 입력이므로 영어를 유지한다.
# [프롬프트 한국어 요약] 단독 애널리스트 역할로, 제공된 데이터만 근거로
# 3~5문장의 근거를 쓰고 마지막 줄에 정확히 "Rating: <5단계 등급>" 형식으로
# 등급을 출력하라는 지시입니다.
BASELINE_SYSTEM_PROMPT = (
    "You are a solo equity analyst making a trading call for the given ticker "
    "as of the given analysis date.\n"
    "Base your judgment ONLY on the data provided below. Do not assume any "
    "knowledge of events after the analysis date.\n\n"
    "Write 3-5 sentences of justification, then end with a final line of "
    "EXACTLY this form (nothing after it):\n"
    f"Rating: <one of {', '.join(RATINGS_5_TIER)}>"
)


def _truncate(text: str, limit: int = MAX_SECTION_CHARS) -> str:
    # 섹션 텍스트를 결정론적으로 앞부분만 자른다(토큰 상한 보호).
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n... (truncated)"


def _fetch_section(label: str, method: str, *args) -> str:
    """dataflows 벤더 라우팅으로 섹션 하나를 수집한다. 실패해도 계속 진행.

    데이터 일부가 없다고 베이스라인 전체를 중단하면 전체 파이프라인(개별
    분석가가 각자 실패를 흡수)과의 비교가 불공정해지므로, 실패는 섹션에
    "(unavailable)" 표시로 남기고 진행합니다.
    """
    try:
        return _truncate(str(route_to_vendor(method, *args)))
    except Exception as e:
        logger.warning("Baseline data fetch failed for %s(%s): %s", method, args, e)
        return f"({label} unavailable: {type(e).__name__}: {e})"


def build_baseline_context(ticker: str, trade_date: str) -> str:
    """주가 요약 + 뉴스 헤드라인으로 구성된 간결한 컨텍스트 문자열을 만든다.

    조회 범위는 모두 ``trade_date``에서 끝나므로(시작일만 과거로 이동),
    벤더 계층의 curr_date 필터와 함께 룩어헤드(미래 정보 유입)를 방지합니다.
    """
    trade_dt = datetime.strptime(trade_date, "%Y-%m-%d")
    price_start = (trade_dt - timedelta(days=PRICE_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    news_start = (trade_dt - timedelta(days=NEWS_LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    price_section = _fetch_section(
        "price data", "get_stock_data", ticker, price_start, trade_date
    )
    news_section = _fetch_section(
        "news headlines", "get_news", ticker, news_start, trade_date
    )

    # 섹션 헤더는 LLM 입력이므로 영어를 유지한다.
    return (
        f"## Price data ({price_start} to {trade_date})\n{price_section}\n\n"
        f"## Recent news ({news_start} to {trade_date})\n{news_section}"
    )


def build_baseline_messages(
    ticker: str, trade_date: str, context: str
) -> list[tuple[str, str]]:
    """LLM invoke()에 넘길 (role, content) 메시지 리스트를 구성한다."""
    human = (
        f"Ticker: {ticker}\n"
        f"Analysis date: {trade_date}\n\n"
        f"{context}\n\n"
        "Give your justification and final rating now."
    )
    return [("system", BASELINE_SYSTEM_PROMPT), ("human", human)]


def create_baseline_llm(config: dict | None = None) -> Any:
    """config의 deep_think_llm으로 베이스라인용 LLM 인스턴스를 생성한다.

    전체 파이프라인의 "깊은 사고" 모델과 같은 모델을 쓰는 이유: 비교 실험에서
    모델 차이가 아닌 아키텍처(단일 호출 vs 멀티 에이전트) 차이만 측정되도록
    하기 위해서입니다.
    """
    cfg = config or DEFAULT_CONFIG
    client = create_llm_client(
        provider=cfg["llm_provider"],
        model=cfg["deep_think_llm"],
        base_url=cfg.get("backend_url"),
    )
    return client.get_llm()


def run_single_llm_baseline(
    ticker: str,
    trade_date: str,
    config: dict | None = None,
    llm: Any = None,
) -> dict:
    """단일 LLM 1회 호출로 5단계 등급을 받는다.

    Args:
        ticker: 분석할 티커
        trade_date: 분석 기준일 (yyyy-mm-dd)
        config: 설정 dict. None이면 DEFAULT_CONFIG 사용
        llm: 이미 생성된 LLM 인스턴스(선택). 배치 실행 시 재사용하거나
            테스트에서 모킹할 때 주입합니다. None이면 config로 생성합니다.

    Returns:
        {"ticker", "trade_date", "rating", "response", "context"} dict.
        rating은 공용 parse_rating으로 추출한 표준 5단계 등급입니다.
    """
    cfg = config or DEFAULT_CONFIG
    # 데이터 벤더 라우팅이 이 config를 따르도록 전역 설정을 갱신
    set_config(cfg)
    if llm is None:
        llm = create_baseline_llm(cfg)

    context = build_baseline_context(ticker, trade_date)
    messages = build_baseline_messages(ticker, trade_date, context)
    response = llm.invoke(messages).content
    rating = parse_rating(response, context="single-LLM baseline")
    return {
        "ticker": ticker,
        "trade_date": trade_date,
        "rating": rating,
        "response": response,
        "context": context,
    }
