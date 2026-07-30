# ============================================================================
# [모듈 개요 - 초보자용]
# 이 파일은 사용자가 입력하는 브로커식 심볼(예: XAUUSD, SPX500, BTCUSD)을
# 야후 파이낸스(Yahoo Finance)가 알아듣는 정식 심볼(GC=F, ^GSPC, BTC-USD)로
# 변환하는 "심볼 정규화(normalization)" 모듈입니다.
# TradingAgents(LLM 멀티 에이전트 주식 트레이딩 프레임워크)의 모든 yfinance
# 데이터 수집 경로가 이 변환을 거쳐야 잘못된 심볼로 빈 데이터를 받는 일을
# 막을 수 있습니다.
# ============================================================================

"""벤더 호출을 위한 심볼 정규화 및 시장 데이터 오류 타입.

야후 파이낸스(기본 벤더)는 사용자가 흔히 입력하는
브로커/TradingView/MT5 스타일 심볼과는 다른 고유한 티커 규칙을 씁니다:

    사용자 입력       야후 표기         이유
    ---------------   ---------------   -----------------------------------
    XAUUSD, XAUUSD+   GC=F              금은 야후에 외환 페어가 없고
                                        COMEX 선물(future)로 호가됨
    EURUSD            EURUSD=X          현물 외환 페어는 ``=X`` 접미사를 붙임
    BTCUSD            BTC-USD           암호화폐 페어는 ``-`` 구분자를 씀
    SPX500, US500     ^GSPC             지수 CFD는 야후 지수 심볼로 매핑됨

브로커 심볼을 그대로 야후에 넘기면 빈 결과가 돌아오는데, 예전에는
에이전트가 이를 자유 텍스트로 받아 그 주변에서 가격을 지어낼(hallucinate)
수 있었습니다(이슈 #781 참고). 매핑을 여기에 중앙화하면 모든 yfinance
진입점이 심볼을 동일한 방식으로 해석하고, 새 종목은 호출부를 고치는 대신
테이블에 행 하나만 추가하면 됩니다.
"""

from __future__ import annotations

import logging
import re

# NoMarketDataError는 벤더 오류 분류 체계(errors.py)에 있습니다;
# normalize_symbol과 함께 임포트하는 많은 호출부를 위해 여기서 재수출합니다.
from .errors import NoMarketDataError as NoMarketDataError

logger = logging.getLogger(__name__)


# 소매 외환 페어에 등장할 만큼 흔한 ISO-4217 통화 코드. 여섯 글자 심볼의
# 앞뒤 절반이 모두 이 집합에 있으면 현물 외환(spot forex) 페어로 간주하고
# 야후의 ``=X`` 접미사를 붙입니다.
_FOREX_CURRENCIES = frozenset(
    {
        "USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD",
        "CNY", "CNH", "HKD", "SGD", "SEK", "NOK", "DKK", "PLN",
        "MXN", "ZAR", "TRY", "INR", "KRW", "BRL", "RUB", "THB",
    }
)

# 브로커들이 구분자 없이 USD와 붙여 호가하는 암호화폐 기초 자산(base).
_CRYPTO_BASES = frozenset(
    {"BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "LTC", "BCH", "DOT", "AVAX", "LINK"}
)

# 규칙만으로는 야후 심볼로 매핑되지 않는 종목들의 명시적 별칭(alias) 테이블.
# 금속/에너지는 최근월물 선물(front-month future)로, 지수 CFD 이름은 기초
# 야후 지수 심볼로 변환합니다. 행만 추가하면 확장됩니다 — 호출부 변경 불필요.
_ALIASES = {
    # 귀금속 (현물 이름 -> COMEX/NYMEX 선물)
    "XAUUSD": "GC=F", "XAU": "GC=F", "GOLD": "GC=F",
    "XAGUSD": "SI=F", "XAG": "SI=F", "SILVER": "SI=F",
    "XPTUSD": "PL=F", "XPDUSD": "PA=F",
    # 에너지
    "WTICOUSD": "CL=F", "USOIL": "CL=F", "WTI": "CL=F",
    "BCOUSD": "BZ=F", "UKOIL": "BZ=F", "BRENT": "BZ=F",
    "NATGAS": "NG=F", "XNGUSD": "NG=F",
    "COPPER": "HG=F", "XCUUSD": "HG=F",
    # 지수 CFD -> 야후 지수 심볼
    "SPX500": "^GSPC", "US500": "^GSPC", "SPX": "^GSPC",
    "NAS100": "^NDX", "US100": "^NDX", "USTEC": "^NDX",
    "US30": "^DJI", "DJI30": "^DJI", "WS30": "^DJI",
    "GER40": "^GDAXI", "GER30": "^GDAXI", "DE40": "^GDAXI",
    "UK100": "^FTSE", "JP225": "^N225", "JPN225": "^N225",
    "FRA40": "^FCHI", "EU50": "^STOXX50E", "HK50": "^HSI",
}

# 야후 심볼에는 영문자, 숫자, 그리고 다음 구조 문자만 올 수 있다.
_YAHOO_SAFE = re.compile(r"^[A-Za-z0-9._\-\^=]+$")


# 모두 야후의 USD 페어로 매핑되는 암호화폐 호가 통화(quote currency). 야후는
# ``<BASE>-USD``만 등록하고 있고(USDT/USDC 스테이블코인 페어는 없음), 이들
# 통화로 호가된 브로커 심볼은 ``-USD``로 변환됩니다(#982). ``USDT``/``USDC``가
# ``USD`` 부분 문자열보다 먼저 매칭되도록 긴 것부터 나열합니다.
_CRYPTO_QUOTES = ("USDT", "USDC", "USD")


def crypto_base(raw: str) -> str | None:
    """USD/USDT/USDC로 호가된 알려진 암호화폐 심볼이 파이프라인에서 가질 수
    있는 어떤 형태(``BTC-USD``, ``BTCUSD``, ``BTC-USDT``)든 받아 기초 자산
    (예: ``BTC``)을 반환하고, 암호화폐가 아니면 None을 반환한다.
    순수하게 문자열 규칙만 사용한다.
    """
    if not isinstance(raw, str):
        return None
    compact = raw.strip().upper().rstrip("+").replace("-", "")
    for quote in _CRYPTO_QUOTES:
        if compact.endswith(quote):
            base = compact[: -len(quote)]
            return base if base in _CRYPTO_BASES else None
    return None


def _normalize_crypto(s: str) -> str | None:
    """USD/USDT/USDC로 호가된 알려진 암호화폐면 ``<BASE>-USD``를, 아니면 None을 반환한다."""
    base = crypto_base(s)
    return f"{base}-USD" if base else None


def normalize_symbol(raw: str) -> str:
    """사용자/브로커 심볼을 정식(canonical) 야후 파이낸스 심볼로 매핑한다.

    해석 순서 (먼저 일치하는 규칙이 이김):
      1. 명시적 별칭 테이블 (금속, 에너지, 지수 CFD).
      2. 암호화폐 규칙: 알려진 암호화폐 기초 자산이 USD/USDT/USDC로
         호가된 경우(대시 유무 무관) -> ``BASE-USD``.
      3. 외환 규칙: ISO 통화 코드 두 개로 이루어진 여섯 글자 -> ``PAIR=X``.
      4. 그 외에는 대문자로 바꾼 심볼을 그대로 반환 (일반 주식, ETF,
         ``GC=F``나 ``^GSPC`` 같은 야후 고유 심볼).

    끝에 붙은 ``+``(브로커 CFD 표시, 예: ``XAUUSD+``)는 매칭 전에
    제거합니다. 이 함수는 순수하게 문자열 규칙만 쓰고 네트워크 호출을 전혀
    하지 않으므로 모든 요청마다 적용해도 안전합니다.
    """
    if not isinstance(raw, str) or not raw.strip():
        return raw

    s = raw.strip().upper()
    # 야후가 절대 쓰지 않는 브로커 CFD/한정자 접미사 제거.
    s = s.rstrip("+")

    crypto = _normalize_crypto(s)
    if s in _ALIASES:
        canonical = _ALIASES[s]
    elif crypto is not None:
        canonical = crypto
    elif len(s) == 6 and s[:3] in _FOREX_CURRENCIES and s[3:] in _FOREX_CURRENCIES:
        canonical = f"{s}=X"
    else:
        canonical = s

    if canonical != raw.strip().upper():
        logger.info("Resolved symbol %r to Yahoo symbol %r", raw, canonical)
    return canonical


def is_yahoo_safe(symbol: str) -> bool:
    """``symbol``이 야후 심볼에 쓰이는 문자만 담고 있으면 True."""
    return bool(symbol) and _YAHOO_SAFE.fullmatch(symbol) is not None
