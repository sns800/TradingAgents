# ============================================================================
# [모듈 개요 - 초보자용]
# 이 파일은 폴리마켓(Polymarket, 예측 시장 플랫폼)에서 미래 이벤트에 대한
# 시장 내재 확률(market-implied probability)을 가져오는 모듈입니다.
# 예측 시장이란 "연준이 금리를 내릴까?" 같은 사건에 사람들이 돈을 걸어
# 형성된 가격이 곧 그 사건의 발생 확률이 되는 시장입니다.
# TradingAgents(LLM 멀티 에이전트 주식 트레이딩 프레임워크)에서 뉴스 분석가
# 에이전트가 거시 이벤트 전망을 참고하는 데이터 소스로 사용합니다.
# ============================================================================

"""Polymarket 예측 시장(prediction-market) 벤더.

미래 지향 이벤트(연준(Fed) 결정, 경기 침체, 선거, 지정학, 암호화폐)에 대한
실시간 시장 내재 확률을 뉴스 분석가에게 제공합니다. 뉴스(무슨 일이
일어났는가), FRED 거시 데이터(현재 상황이 어떤가)를 보완하여,
군중이 실제로 다음에 일어난다고 가격에 반영한 것을 보여줍니다.

Polymarket의 공개 Gamma API(https://gamma-api.polymarket.com)를 사용합니다 —
키도 인증도 필요 없습니다. 각 시장의 ``outcomePrices``는 결과(outcome)별
내재 확률입니다("Yes"가 0.76이면 시장이 76% 확률로 가격을 매겼다는 뜻).
"""
import json
import logging
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"

# 네트워크 타임아웃(초). 다른 벤더들과 일관되게 맞춘 값.
REQUEST_TIMEOUT = 30

# 반환할 기본 시장 개수. 거래량(traded volume) 순으로 순위를 매긴다.
DEFAULT_LIMIT = 6


def _request(path: str, params: dict) -> dict:
    response = requests.get(
        f"{GAMMA_BASE}/{path}", params=params, timeout=REQUEST_TIMEOUT
    )
    response.raise_for_status()
    return response.json()


def _parse_json_list(value) -> list:
    """Gamma는 ``outcomes``/``outcomePrices``를 JSON 문자열 배열로 인코딩한다."""
    if isinstance(value, list):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []


def _is_forward_looking(market: dict, now: datetime) -> bool:
    """미래에 결판나는(resolve) 열린 시장만 남긴다.

    ``closed``가 신뢰할 수 있는 '결판남' 플래그입니다(``active``는 이미
    정산된 시장에서도 True로 남음). 그리고 ``endDate``가 과거라면 이벤트가
    이미 결판난 것입니다 — 어느 쪽이든 미래 지향 신호가 아닙니다.
    """
    if market.get("closed"):
        return False
    end_date = market.get("endDate")
    if end_date:
        try:
            if datetime.fromisoformat(end_date.replace("Z", "+00:00")) < now:
                return False
        except ValueError:
            pass
    return bool(_parse_json_list(market.get("outcomePrices"))) and bool(
        _parse_json_list(market.get("outcomes"))
    )


def get_prediction_markets(topic: str, limit: int | None = None) -> str:
    """이벤트 주제에 대한 실시간 예측 시장 확률을 반환한다.

    Args:
        topic: 이벤트 키워드. 예: "Fed rate cut", "recession 2026",
            "US election", 또는 섹터/기업 이벤트.
        limit: 반환할 최대 시장 수(거래량 순 정렬); ``None``이면
            DEFAULT_LIMIT을 사용.

    Returns:
        주제와 일치하는, 거래량이 가장 많은 열린 시장들의 마크다운 보고서.
        각 시장에 내재 확률, 거래량, 결판 날짜, 최근(1주) 변동폭이 담긴다.
    """
    if limit is None:
        limit = DEFAULT_LIMIT

    try:
        data = _request("public-search", {"q": topic, "limit_per_type": 20})
    except requests.RequestException as e:
        logger.warning("Polymarket search failed for %r: %s", topic, e)
        return (
            f"Polymarket data is currently unavailable (network error: {e}). "
            f"Proceed without prediction-market signal for '{topic}'."
        )

    now = datetime.now(timezone.utc)
    candidates = [
        m
        for event in data.get("events", [])
        for m in event.get("markets", [])
        if _is_forward_looking(m, now)
    ]
    candidates.sort(key=lambda m: m.get("volumeNum") or 0, reverse=True)

    header = (
        f'## Polymarket prediction markets: "{topic}"\n'
        f"Live, market-implied probabilities (higher traded volume = deeper, "
        f"more reliable). A probability is the crowd's priced odds of the event, "
        f"not a forecast you should take as certain.\n\n"
    )

    if not candidates:
        return header + (
            f"No open prediction markets matched '{topic}'. Polymarket coverage "
            f"is concentrated in macro, political, geopolitical, and crypto "
            f"events; a specific equity may have none."
        )

    lines = []
    for m in candidates[:limit]:
        prices = _parse_json_list(m.get("outcomePrices"))
        outcomes = _parse_json_list(m.get("outcomes"))
        try:
            prob = float(prices[0])
        except (ValueError, IndexError):
            continue
        label = outcomes[0] if outcomes else "Yes"
        volume = m.get("volumeNum") or 0
        end_date = (m.get("endDate") or "")[:10]
        wk = m.get("oneWeekPriceChange")
        # 1주간 확률 변동을 퍼센트포인트(pp)로 표기한다.
        wk_str = (
            f", 1-week {wk * 100:+.1f}pp"
            if isinstance(wk, (int, float)) and wk
            else ""
        )
        lines.append(
            f"- **{m.get('question')}** — {label} {prob:.0%} "
            f"(${volume:,.0f} volume, resolves {end_date}{wk_str})"
        )

    return header + "\n".join(lines) + "\n"
