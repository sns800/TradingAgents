# [모듈 개요 - 초보자용]
# 이 파일은 TradingAgents 전체의 기본 설정(DEFAULT_CONFIG)을 정의합니다.
# 어떤 LLM 제공자/모델을 쓸지, 토론을 몇 라운드 할지, 데이터를 어느 벤더에서
# 받아올지, 결과를 어디에 저장할지 등 시스템의 동작 방식을 결정하는 값들이
# 모두 여기에 있습니다. trading_graph.py를 비롯한 대부분의 모듈이 이 딕셔너리를
# 읽어 동작하며, TRADINGAGENTS_* 환경 변수(.env)로 개별 값을 덮어쓸 수 있습니다.
# 주의: 설정 키(key) 문자열은 코드 전반에서 참조되므로 절대 바꾸면 안 됩니다.

import os

_TRADINGAGENTS_HOME = os.path.join(os.path.expanduser("~"), ".tradingagents")

# 환경 변수 -> 설정 키 덮어쓰기(override)의 단일 관리 지점(single source of truth).
# 새 설정 키를 환경 변수로 노출하려면 여기에 한 줄만 추가하면 되고,
# 진입점(entry-point) 스크립트는 고칠 필요가 없습니다. 값의 형 변환(coercion)은
# 기존 기본값의 타입을 따르므로, 사용자는 .env 파일에 평범한 문자열만 쓰면 됩니다.
_ENV_OVERRIDES = {
    "TRADINGAGENTS_LLM_PROVIDER":         "llm_provider",
    "TRADINGAGENTS_DEEP_THINK_LLM":       "deep_think_llm",
    "TRADINGAGENTS_QUICK_THINK_LLM":      "quick_think_llm",
    "TRADINGAGENTS_LLM_BACKEND_URL":      "backend_url",
    "TRADINGAGENTS_OUTPUT_LANGUAGE":      "output_language",
    "TRADINGAGENTS_MAX_DEBATE_ROUNDS":    "max_debate_rounds",
    "TRADINGAGENTS_MAX_RISK_ROUNDS":      "max_risk_discuss_rounds",
    "TRADINGAGENTS_CHECKPOINT_ENABLED":   "checkpoint_enabled",
    "TRADINGAGENTS_BENCHMARK_TICKER":     "benchmark_ticker",
    "TRADINGAGENTS_TEMPERATURE":          "temperature",
    "TRADINGAGENTS_LLM_MAX_RETRIES":      "llm_max_retries",
    # 제공자(provider)별 추론/사고(reasoning/thinking) 조절 옵션
    # (None = 각 제공자의 자체 기본값 사용). 비대화형(non-interactive) 실행을
    # 위해 여기서 설정할 수 있으며, CLI도 대화형 선택지를 제공하지만
    # 해당 환경 변수가 설정되어 있으면 그 단계를 건너뜁니다.
    "TRADINGAGENTS_GOOGLE_THINKING_LEVEL":   "google_thinking_level",
    "TRADINGAGENTS_OPENAI_REASONING_EFFORT": "openai_reasoning_effort",
    "TRADINGAGENTS_ANTHROPIC_EFFORT":        "anthropic_effort",
}


_BOOL_TRUE = ("true", "1", "yes", "on")
_BOOL_FALSE = ("false", "0", "no", "off")


def _coerce(value: str, reference):
    """환경 변수 문자열을 기존 기본값의 타입에 맞게 변환한다.

    잘못된 값은 조용히 기본값으로 되돌리지 않고 ``ValueError``를 던집니다 —
    오타 난 불리언(예: ``treu``)이나 숫자가 아닌 정수 값은 무인(unattended)
    실행을 조용히 잘못 설정하는 대신, 시작 시점에 크게 실패해야 합니다.
    """
    if isinstance(reference, bool):
        normalized = value.strip().lower()
        if normalized in _BOOL_TRUE:
            return True
        if normalized in _BOOL_FALSE:
            return False
        raise ValueError(
            f"expected a boolean ({'/'.join(_BOOL_TRUE + _BOOL_FALSE)}), got {value!r}"
        )
    if isinstance(reference, int) and not isinstance(reference, bool):
        return int(value)
    if isinstance(reference, float):
        return float(value)
    return value


def _apply_env_overrides(config: dict) -> dict:
    """TRADINGAGENTS_* 환경 변수를 설정 딕셔너리에 제자리(in-place)로 적용한다."""
    for env_var, key in _ENV_OVERRIDES.items():
        raw = os.environ.get(env_var)
        if raw is None or raw == "":
            continue
        try:
            config[key] = _coerce(raw, config.get(key))
        except ValueError as exc:
            raise ValueError(f"Invalid value for {env_var}: {exc}") from exc
    return config


DEFAULT_CONFIG = _apply_env_overrides({
    # 프로젝트 루트 디렉터리 (이 파일이 있는 위치의 절대 경로)
    "project_dir": os.path.abspath(os.path.join(os.path.dirname(__file__), ".")),
    # 분석 결과(보고서·로그)가 저장되는 디렉터리
    "results_dir": os.getenv("TRADINGAGENTS_RESULTS_DIR", os.path.join(_TRADINGAGENTS_HOME, "logs")),
    # 데이터 캐시(시세·뉴스 등 조회 결과)와 체크포인트가 저장되는 디렉터리
    "data_cache_dir": os.getenv("TRADINGAGENTS_CACHE_DIR", os.path.join(_TRADINGAGENTS_HOME, "cache")),
    # 매매 결정·리플렉션이 누적되는 메모리 로그(memory log) 파일 경로
    "memory_log_path": os.getenv("TRADINGAGENTS_MEMORY_LOG_PATH", os.path.join(_TRADINGAGENTS_HOME, "memory", "trading_memory.md")),
    # 해소된(resolved) 메모리 로그 항목 개수의 상한(선택). 값을 설정하면
    # 이 한도를 넘었을 때 가장 오래된 해소 항목부터 정리(prune)됩니다.
    # 아직 결과 대기 중(pending)인 항목은 절대 정리되지 않습니다.
    # None이면 로테이션(rotation)을 완전히 끕니다.
    "memory_log_max_entries": None,
    # ----- LLM 설정 -----
    # 사용할 LLM 제공자(provider): "openai", "google", "anthropic" 등
    "llm_provider": "openai",
    # 깊은 사고용 모델: 리서치 매니저·포트폴리오 매니저처럼 중요한 종합 판단에 사용
    "deep_think_llm": "gpt-5.5",
    # 빠른 사고용 모델: 애널리스트·토론자처럼 호출 횟수가 많은 에이전트에 사용
    "quick_think_llm": "gpt-5.4-mini",
    # LLM API 엔드포인트 URL. None이면 각 제공자의 클라이언트가 자체 기본
    # 엔드포인트를 사용합니다 (OpenAI는 api.openai.com, Gemini는
    # generativelanguage.googleapis.com 등). CLI는 사용자가 제공자를 고르면
    # 제공자별로 이 값을 덮어씁니다. 특정 제공자용 URL을 여기 남겨 두면
    # 다른 제공자로 새어 나갈 수 있습니다 (예: 예전에 OpenAI의 /v1이 Gemini로
    # 전달되어 잘못된 요청 URL이 만들어진 적이 있습니다).
    "backend_url": None,
    # ----- 제공자별 사고(thinking) 설정 -----
    "google_thinking_level": None,      # Google Gemini의 사고 수준: "high", "minimal" 등
    "openai_reasoning_effort": None,    # OpenAI 추론 강도: "medium", "high", "low"
    "anthropic_effort": None,           # Anthropic 사고 강도: "high", "medium", "low"
    # 샘플링 온도(temperature). 값이 설정되면 모든 제공자에 전달됩니다.
    # None이면 제공자별 기본값을 그대로 둡니다. 낮은 값은 이를 지원하는
    # 모델에서 실행 간 편차를 줄이지만, 추론(reasoning) 모델은 대부분 이 값을
    # 무시하며 어떤 설정으로도 LLM 출력이 실행마다 비트 단위로 동일해지지는
    # 않습니다 (README 참고).
    "temperature": None,
    # 모든 제공자 채팅 클라이언트에 전달되는 SDK 재시도(retry) 횟수 한도.
    # None이면 각 제공자/SDK의 기본값(보통 2)을 그대로 둡니다. 속도 제한이
    # 걸린 배포 환경에서 몰아치는 429 스로틀링(throttling)을 실행 중단 대신
    # 견뎌 내려면 값을 올리세요 (#1091).
    "llm_max_retries": None,
    # 체크포인트/재개(resume): True면 LangGraph가 노드 하나가 끝날 때마다
    # 상태를 저장하므로, 도중에 죽은 실행을 마지막 성공 단계부터 재개할 수 있습니다.
    "checkpoint_enabled": False,
    # 애널리스트 보고서와 최종 결정의 출력 언어.
    # 내부 에이전트 토론은 추론 품질을 위해 영어로 유지됩니다.
    # ※ 한글화 포크: 원본의 기본값은 "English"이지만 이 포크는 "Korean"입니다.
    #    TRADINGAGENTS_OUTPUT_LANGUAGE 환경변수로 언제든 바꿀 수 있습니다.
    "output_language": "Korean",
    # ----- 토론(debate) 관련 설정 -----
    # 강세/약세 연구원 토론의 최대 라운드 수
    "max_debate_rounds": 1,
    # 리스크 3자 토론의 최대 라운드 수
    "max_risk_discuss_rounds": 1,
    # LangGraph 재귀 한도(recursion limit): 그래프가 무한 루프에 빠지지 않도록
    # 노드 실행 횟수를 제한합니다
    "max_recur_limit": 100,
    # ----- 뉴스/데이터 수집 파라미터 -----
    # 더 긴 되돌아보기(lookback) 전략이나 거시(macro) 커버리지를 넓히려면 늘리고,
    # 에이전트 프롬프트의 토큰 사용량을 줄이려면 줄이세요.
    "news_article_limit": 20,             # 티커별 뉴스 기사 최대 개수 (ticker-news)
    "global_news_article_limit": 10,      # 글로벌/거시 뉴스 기사 최대 개수
    "global_news_lookback_days": 7,       # 거시 뉴스를 되돌아보는 기간(일)
    # get_global_news가 거시 헤드라인을 찾을 때 사용하는 검색 쿼리 목록.
    # 지역/섹터 커버리지를 넓히려면 항목을 추가하거나 교체하세요.
    # (검색어 문자열 자체는 프로그램 동작에 쓰이므로 영어를 유지합니다.)
    "global_news_queries": [
        "Federal Reserve interest rates inflation",
        "S&P 500 earnings GDP economic outlook",
        "geopolitical risk trade war sanctions",
        "ECB Bank of England BOJ central bank policy",
        "oil commodities supply chain energy",
    ],
    # ----- 데이터 벤더(vendor) 설정 -----
    # 카테고리 수준 설정 (해당 카테고리의 모든 도구에 대한 기본값).
    # 설정한 값이 곧 정확한 벤더 체인(chain)입니다 — 요청이 사용자가 고르지
    # 않은 벤더로 조용히 우회되지 않습니다. 순서 있는 대체(fallback)를 원하면
    # 여러 개를 나열하세요 (예: "yfinance,alpha_vantage").
    # "default"는 사용 가능한 모든 벤더를 사용합니다.
    "data_vendors": {
        "core_stock_apis": "yfinance",       # 주가 데이터. 선택지: alpha_vantage, yfinance
        "technical_indicators": "yfinance",  # 기술적 지표. 선택지: alpha_vantage, yfinance
        "fundamental_data": "yfinance",      # 재무제표 데이터. 선택지: alpha_vantage, yfinance
        "news_data": "yfinance",             # 뉴스 데이터. 선택지: alpha_vantage, yfinance
        "macro_data": "fred",                # 거시 지표. 선택지: fred (FRED_API_KEY 필요)
        "prediction_markets": "polymarket",  # 예측 시장. 선택지: polymarket (API 키 불필요)
    },
    # 도구(tool) 수준 설정 (카테고리 수준 설정보다 우선 적용됨)
    "tool_vendors": {
        # 예시: "get_stock_data": "alpha_vantage",  # 카테고리 기본값을 덮어씀
    },
    # ----- 리플렉션 계층의 알파(alpha, 벤치마크 대비 초과 수익) 계산용 벤치마크 -----
    # ``benchmark_ticker``가 설정되면 모든 티커에 대해 접미사 매핑보다
    # 우선합니다. None으로 두면 티커의 거래소 접미사(suffix)를 기준으로
    # ``benchmark_map``이 자동 감지합니다. 미국 티커의 기본값은 SPY로 유지되어
    # 리플렉션 라벨이 계속 "Alpha vs SPY"로 표기되고, 미국 외 티커는
    # 자동으로 해당 지역 지수를 사용합니다.
    "benchmark_ticker": None,
    # 거래소 접미사 -> 벤치마크 지수 매핑
    "benchmark_map": {
        ".NS":  "^NSEI",       # 인도 NSE (Nifty 50)
        ".BO":  "^BSESN",      # 인도 BSE (Sensex)
        ".T":   "^N225",       # 도쿄 (닛케이 225)
        ".HK":  "^HSI",        # 홍콩 (항셍)
        ".L":   "^FTSE",       # 런던 (FTSE 100)
        ".TO":  "^GSPTSE",     # 토론토 (TSX 종합)
        ".AX":  "^AXJO",       # 호주 (ASX 200)
        ".SS":  "000001.SS",   # 상하이 (SSE 종합)
        ".SZ":  "399001.SZ",   # 선전 (SZSE 성분)
        "":     "SPY",         # 접미사 없는 미국 상장 티커의 기본값
    },
})
