# ============================================================================
# [모듈 개요 - 초보자용]
# 이 파일은 스톡트윗츠(StockTwits, 주식 전용 SNS)에서 특정 종목에 대한
# 최근 게시글과 투자 심리(Bullish=강세/Bearish=약세) 라벨을 가져오는 모듈입니다.
# TradingAgents(LLM 멀티 에이전트 주식 트레이딩 프레임워크)에서 소셜 미디어
# 분석가 에이전트가 시장의 대중 심리를 파악하는 데이터 소스로 사용합니다.
# API 키 없이 공개 엔드포인트를 호출하며, 실패 시에도 예외 대신
# 안내 문자열을 돌려주어 호출자가 항상 문자열만 다루면 되게 합니다.
# ============================================================================

"""StockTwits 공개 심볼 스트림(symbol-stream) 수집기.

StockTwits는 ``api.stocktwits.com/api/2/streams/symbol/{ticker}.json`` 에서
심볼별 메시지 스트림을 제공하며, API 키·OAuth·가입이 전혀 필요 없습니다.
각 메시지에는 사용자가 직접 붙인 심리 필드(``Bullish``/``Bearish``/null),
본문, 타임스탬프, 작성자가 담겨 있습니다.

이 함수는 의도적으로 자기완결적으로 만들었습니다: 짧은 타임아웃,
HTTP·파싱 실패 시의 우아한 성능 저하(graceful degradation), 그리고
네트워크 호출 성공 여부와 무관하게 호출 에이전트가 동일한 인터페이스를
받도록 문자열 반환 타입을 사용합니다.
"""

from __future__ import annotations

import http.client
import json
import logging
from urllib.request import Request, urlopen

from .symbol_utils import crypto_base

logger = logging.getLogger(__name__)

_API = "https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"
_UA = "tradingagents/0.2 (+https://github.com/TauricResearch/TradingAgents)"


def _stocktwits_symbol(ticker: str) -> str:
    """암호화폐 페어를 StockTwits의 ``<BASE>.X`` 표기 규칙으로 변환한다.

    StockTwits는 암호화폐를 ``BTC.X`` 형태로 등록하고 있어서(야후의
    ``BTC-USD`` 형태는 404가 남), 암호화폐 심볼은 모두 기초 자산(base) +
    ``.X`` 로 변환하고, 그 외 심볼은 대문자로만 바꿔 그대로 통과시킵니다.
    """
    base = crypto_base(ticker)
    return f"{base}.X" if base else ticker.strip().upper()


def fetch_stocktwits_messages(ticker: str, limit: int = 30, timeout: float = 10.0) -> str:
    """``ticker``의 최근 StockTwits 메시지를 가져와, 프롬프트에 바로 넣을 수
    있는 형식의 일반 텍스트 블록으로 반환한다.

    엔드포인트에 접속할 수 없거나, 심볼에 메시지가 없거나, 응답 형태가
    예상과 다를 때는 자리표시(placeholder) 문자열을 반환합니다 — 호출자가
    None이나 예외를 따로 처리할 필요가 전혀 없습니다.
    """
    url = _API.format(ticker=_stocktwits_symbol(ticker))
    req = Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except (OSError, http.client.HTTPException, json.JSONDecodeError) as exc:
        # OSError는 URLError/TimeoutError/연결 리셋을 포괄하고, HTTPException은
        # 청크 전송(chunked-transfer) 오류(IncompleteRead/BadStatusLine, #1024)를 포괄합니다.
        logger.warning("StockTwits fetch failed for %s: %s", ticker, exc)
        return f"<stocktwits unavailable: {type(exc).__name__}>"

    messages = data.get("messages", []) if isinstance(data, dict) else []
    if not messages:
        return f"<no StockTwits messages found for ${ticker.upper()}>"

    lines = []
    bullish = bearish = unlabeled = 0
    for m in messages[:limit]:
        created = m.get("created_at", "")
        user = (m.get("user") or {}).get("username", "?")
        entities = m.get("entities") or {}
        sentiment_obj = entities.get("sentiment") or {}
        sentiment = sentiment_obj.get("basic") if isinstance(sentiment_obj, dict) else None
        body = (m.get("body") or "").replace("\n", " ").strip()
        if len(body) > 280:
            body = body[:280] + "…"

        # 심리 라벨별 집계: Bullish(강세)/Bearish(약세)/라벨 없음
        if sentiment == "Bullish":
            bullish += 1
            tag = "Bullish"
        elif sentiment == "Bearish":
            bearish += 1
            tag = "Bearish"
        else:
            unlabeled += 1
            tag = "no-label"
        lines.append(f"[{created} · @{user} · {tag}] {body}")

    total = bullish + bearish + unlabeled
    bull_pct = round(100 * bullish / total) if total else 0
    bear_pct = round(100 * bearish / total) if total else 0
    summary = (
        f"Bullish: {bullish} ({bull_pct}%) · "
        f"Bearish: {bearish} ({bear_pct}%) · "
        f"Unlabeled: {unlabeled} · "
        f"Total: {total} most-recent messages"
    )
    return summary + "\n\n" + "\n".join(lines)
