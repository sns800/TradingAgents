# ============================================================================
# [모듈 개요 - 초보자용]
# 이 파일은 데이터 요청을 알맞은 벤더(vendor, 데이터 제공사)로 연결해 주는
# "벤더 라우팅(routing)" 계층입니다. 예컨대 에이전트가 "주가 데이터를 달라"고
# 하면, 설정에 따라 야후 파이낸스(yfinance)나 알파 밴티지(alpha_vantage) 중
# 어느 구현을 호출할지 결정하고, 실패 시 다음 벤더로 넘어가는 폴백(fallback)
# 까지 처리합니다. TradingAgents(LLM 멀티 에이전트 주식 트레이딩 프레임워크)의
# 모든 데이터 도구 호출이 이 파일의 route_to_vendor()를 거칩니다.
# ============================================================================

import logging

from .alpha_vantage import (
    get_balance_sheet as get_alpha_vantage_balance_sheet,
    get_cashflow as get_alpha_vantage_cashflow,
    get_fundamentals as get_alpha_vantage_fundamentals,
    get_global_news as get_alpha_vantage_global_news,
    get_income_statement as get_alpha_vantage_income_statement,
    get_indicator as get_alpha_vantage_indicator,
    get_insider_transactions as get_alpha_vantage_insider_transactions,
    get_news as get_alpha_vantage_news,
    get_stock as get_alpha_vantage_stock,
)
from .config import get_config
from .errors import (
    NoMarketDataError,
    VendorNotConfiguredError,
    VendorRateLimitError,
)
from .fred import get_macro_data as get_fred_macro_data
from .polymarket import get_prediction_markets as get_polymarket_prediction_markets
from .y_finance import (
    get_balance_sheet as get_yfinance_balance_sheet,
    get_cashflow as get_yfinance_cashflow,
    get_fundamentals as get_yfinance_fundamentals,
    get_income_statement as get_yfinance_income_statement,
    get_insider_transactions as get_yfinance_insider_transactions,
    get_stock_stats_indicators_window,
    get_YFin_data_online,
)
from .yfinance_news import get_global_news_yfinance, get_news_yfinance

logger = logging.getLogger(__name__)

# NO_DATA 센티널의 기계 판독용 접두사. route_to_vendor()가 "설정된 모든 벤더에
# 데이터 없음"을 확인했을 때 반환하는 센티널 문자열이 이 접두사로 시작합니다.
# 결정론적 게이트(설계분석 중기 로드맵 #4)가 LLM 판단 없이 이 문자열 검사만으로
# 데이터 부재를 감지합니다: 시장 분석가가 도구 결과에서 센티널을 발견하면
# 상태의 market_data_ok 플래그를 False로 내리고, 포트폴리오 매니저는 LLM 호출
# 없이 강제 Hold로 분기합니다.
NO_DATA_SENTINEL_PREFIX = "NO_DATA_AVAILABLE"


def is_no_data_sentinel(text) -> bool:
    """도구 결과 텍스트에 NO_DATA 센티널이 들어 있는지 결정론적으로 검사한다.

    프롬프트 순응("데이터 없이 값을 지어내지 마라")은 확률적 방어일 뿐이므로,
    자금이 걸린 결정은 이 문자열 검사 같은 결정론적 로직으로 게이트합니다.
    선택적 부가 데이터(뉴스·매크로 등)가 쓰는 "DATA_UNAVAILABLE" 완화 센티널은
    핵심 데이터 부재가 아니므로 여기 매칭되지 않습니다.
    """
    return isinstance(text, str) and NO_DATA_SENTINEL_PREFIX in text

# 카테고리별로 정리한 도구 목록
TOOLS_CATEGORIES = {
    "core_stock_apis": {
        # OHLCV = 시가(Open)·고가(High)·저가(Low)·종가(Close)·거래량(Volume)
        "description": "OHLCV stock price data",
        "tools": [
            "get_stock_data"
        ]
    },
    "technical_indicators": {
        "description": "Technical analysis indicators",
        "tools": [
            "get_indicators"
        ]
    },
    "fundamental_data": {
        "description": "Company fundamentals",
        "tools": [
            "get_fundamentals",
            "get_balance_sheet",
            "get_cashflow",
            "get_income_statement"
        ]
    },
    "news_data": {
        "description": "News and insider data",
        "tools": [
            "get_news",
            "get_global_news",
            "get_insider_transactions",
        ]
    },
    "macro_data": {
        "description": "Macroeconomic indicators (rates, inflation, labor, growth)",
        "tools": [
            "get_macro_indicators",
        ]
    },
    "prediction_markets": {
        "description": "Market-implied probabilities for forward-looking events",
        "tools": [
            "get_prediction_markets",
        ]
    }
}

VENDOR_LIST = [
    "yfinance",
    "fred",
    "polymarket",
    "alpha_vantage",
]

# 선택적(optional) 부가 정보 카테고리. 뉴스 분석가에게 거시/이벤트 맥락을
# 더해 주지만 의사결정의 핵심은 아니므로, 여기서 벤더가 실패하면 실행을
# 중단하는 대신 안내 문자열(sentinel)로 완화합니다(LLM이 잘못 넘긴 지표,
# 누락된 키, 일시적 네트워크 문제 때문에 곁가지 데이터가 분석 전체를
# 죽여서는 안 됩니다). 핵심 카테고리(가격, 재무, 뉴스)는 여전히 예외를
# 던져 주요 소스 고장이 크게 드러나게 합니다.
OPTIONAL_CATEGORIES = {"macro_data", "prediction_markets"}

# 각 메서드를 벤더별 구현에 매핑하는 테이블
VENDOR_METHODS = {
    # core_stock_apis (핵심 주가 API)
    "get_stock_data": {
        "alpha_vantage": get_alpha_vantage_stock,
        "yfinance": get_YFin_data_online,
    },
    # technical_indicators (기술적 지표)
    "get_indicators": {
        "alpha_vantage": get_alpha_vantage_indicator,
        "yfinance": get_stock_stats_indicators_window,
    },
    # fundamental_data (기업 재무 데이터)
    "get_fundamentals": {
        "alpha_vantage": get_alpha_vantage_fundamentals,
        "yfinance": get_yfinance_fundamentals,
    },
    "get_balance_sheet": {
        "alpha_vantage": get_alpha_vantage_balance_sheet,
        "yfinance": get_yfinance_balance_sheet,
    },
    "get_cashflow": {
        "alpha_vantage": get_alpha_vantage_cashflow,
        "yfinance": get_yfinance_cashflow,
    },
    "get_income_statement": {
        "alpha_vantage": get_alpha_vantage_income_statement,
        "yfinance": get_yfinance_income_statement,
    },
    # news_data (뉴스 데이터)
    "get_news": {
        "alpha_vantage": get_alpha_vantage_news,
        "yfinance": get_news_yfinance,
    },
    "get_global_news": {
        "yfinance": get_global_news_yfinance,
        "alpha_vantage": get_alpha_vantage_global_news,
    },
    "get_insider_transactions": {
        "alpha_vantage": get_alpha_vantage_insider_transactions,
        "yfinance": get_yfinance_insider_transactions,
    },
    # macro_data (거시경제 데이터)
    "get_macro_indicators": {
        "fred": get_fred_macro_data,
    },
    # prediction_markets (예측 시장)
    "get_prediction_markets": {
        "polymarket": get_polymarket_prediction_markets,
    },
}

def get_category_for_method(method: str) -> str:
    """지정한 메서드가 속한 카테고리를 반환한다."""
    for category, info in TOOLS_CATEGORIES.items():
        if method in info["tools"]:
            return category
    raise ValueError(f"Method '{method}' not found in any category")

def get_vendor(category: str, method: str = None) -> str:
    """데이터 카테고리 또는 특정 도구 메서드에 설정된 벤더를 반환한다.
    도구 단위 설정이 카테고리 단위 설정보다 우선한다.
    """
    config = get_config()

    # (method가 주어졌다면) 도구 단위 설정을 먼저 확인
    if method:
        tool_vendors = config.get("tool_vendors", {})
        if method in tool_vendors:
            return tool_vendors[method]

    # 없으면 카테고리 단위 설정으로 폴백(fallback)
    return config.get("data_vendors", {}).get(category, "default")

def route_to_vendor(method: str, *args, **kwargs):
    """메서드 호출을 알맞은 벤더 구현으로 라우팅하고, 폴백을 지원한다."""
    category = get_category_for_method(method)
    vendor_config = get_vendor(category, method)
    primary_vendors = [v.strip() for v in vendor_config.split(',')]

    if method not in VENDOR_METHODS:
        raise ValueError(f"Method '{method}' not supported")

    all_available_vendors = list(VENDOR_METHODS[method].keys())

    # 설정된 벤더 목록이 곧 폴백 체인(chain)입니다: 사용자가 선택하지 않은
    # 벤더로 몰래 폴백하지 않습니다(#988/#289) — 그렇게 하면 예상치 못한
    # 출처의 데이터가 반환되어 벤더 간 불일치를 일으켰습니다. 다중 벤더
    # 폴백이 필요하면 순서대로 나열하세요. 예: data_vendors="yfinance,alpha_vantage".
    # "default" 센티널(명시적 설정 없음)은 사용 가능한 모든 벤더를 씁니다.
    explicit = [v for v in primary_vendors if v and v != "default"]
    if explicit:
        vendor_chain = [v for v in explicit if v in VENDOR_METHODS[method]]
        if not vendor_chain:
            raise ValueError(
                f"Configured vendor(s) {explicit} not available for '{method}'. "
                f"Available: {all_available_vendors}."
            )
    else:
        vendor_chain = all_available_vendors

    last_no_data: NoMarketDataError | None = None
    first_error: Exception | None = None
    for vendor in vendor_chain:
        vendor_impl = VENDOR_METHODS[method][vendor]
        impl_func = vendor_impl[0] if isinstance(vendor_impl, list) else vendor_impl

        try:
            return impl_func(*args, **kwargs)
        except VendorRateLimitError:
            logger.warning("Vendor %r rate-limited for %s; trying next vendor.", vendor, method)
            continue
        except VendorNotConfiguredError as e:
            logger.warning("Vendor %r not configured for %s; trying next vendor.", vendor, method)
            if first_error is None:
                first_error = e  # 다른 어떤 벤더도 이 호출을 처리하지 못하면 이 오류를 드러낸다.
            continue
        except NoMarketDataError as e:
            last_no_data = e  # 이 벤더엔 데이터가 없음; 설정된 다른 벤더에는 있을 수도 있다
            continue
        except Exception as e:
            # 다른 벤더가 처리할 수 있는데 한 벤더의 실패로 호출 전체가
            # 죽지 않게 하되, 조용히 삼키지도 않습니다: 주요 소스 고장은
            # 폴백의 결과 뒤에 숨겨지지 않고 로그에 보여야 합니다(#989).
            logger.warning("Vendor %r failed for %s: %s", vendor, method, e)
            if first_error is None:
                first_error = e
            continue

    # 어떤 벤더든 "데이터 없음"을 보고했다면 그 심볼은 정말로 조회 불가입니다.
    # 벤더마다 다른 빈 문자열 대신, 명시적이고 지시적인 센티널(sentinel) 하나를
    # 반환하여 에이전트가 값을 지어내지 않고 "이용 불가"라고 보고하게 합니다.
    # 이 처리는 폴백 과정에서 우연히 발생한 오류보다 우선합니다.
    if last_no_data is not None:
        if first_error is not None:
            # 어떤 벤더는 실제 오류도 냈습니다; 데이터-없음 결론이 주요 소스의
            # 고장(네트워크/인증 등)을 가리지 못하도록 로그로 드러냅니다.
            logger.warning(
                "Returning NO_DATA for %s, but a vendor errored earlier: %s",
                method, first_error,
            )
        sym = last_no_data.symbol
        canonical = last_no_data.canonical
        resolved = "" if canonical == sym else f" (resolved to '{canonical}')"
        # 타입 있는 오류의 상세 내용(예: "latest row is 2025-06-11 ... stale")을
        # 드러내어, 에이전트가 막연한 "이용 불가"가 아니라 구체적인 이유 —
        # 잘못된 심볼, 커버리지 없음, 오래된(stale) 데이터 — 를 보게 합니다.
        reason = f" ({last_no_data.detail})" if last_no_data.detail else ""
        return (
            f"{NO_DATA_SENTINEL_PREFIX}: No usable market data for '{sym}'{resolved} from "
            f"any configured vendor{reason}. The symbol may be invalid, delisted, "
            f"not covered, or the vendor returned stale data. Do not estimate or "
            f"fabricate values — report that data is unavailable for this symbol."
        )

    # 데이터를 반환한 벤더도, 깨끗한 "데이터 없음"을 보고한 벤더도 없는 경우 —
    # 첫 번째 실제 오류(예: 주요 벤더의 네트워크 실패)를 드러냅니다. 단,
    # 선택적 부가 정보 카테고리는 센티널로 완화하여, 곁가지 데이터가 실행을
    # 중단시키지 못하게 합니다.
    if first_error is not None:
        if category in OPTIONAL_CATEGORIES:
            logger.warning("Optional %s unavailable for %s: %s", category, method, first_error)
            return (
                f"DATA_UNAVAILABLE: optional {category} could not be retrieved "
                f"({first_error}). Proceed without it; do not fabricate values."
            )
        raise first_error

    raise RuntimeError(f"No available vendor for '{method}'")
