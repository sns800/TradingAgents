# ============================================================
# [모듈 개요] TradingAgents CLI의 대화형 입력 유틸리티 모음
# TradingAgents(LLM 멀티 에이전트 주식 트레이딩 프레임워크)의 CLI가
# 분석을 시작하기 전에 필요한 값들 — 종목 코드, 분석 날짜, 애널리스트 팀,
# LLM 공급자/모델, API 키, 출력 언어 등 — 을 questionary 라이브러리로
# 사용자에게 물어보는 함수들이 들어 있다. cli/main.py에서 호출된다.
# ============================================================
import os
from pathlib import Path

import questionary
from dotenv import find_dotenv, set_key
from rich.console import Console

from cli.models import AnalystType, AssetType
from tradingagents.llm_clients.api_key_env import get_api_key_env
from tradingagents.llm_clients.model_catalog import get_model_options

console = Console()

TICKER_INPUT_EXAMPLES = "SPY, 0700.HK, BTC-USD"

ANALYST_ORDER = [
    ("시장 분석가 (Market Analyst)", AnalystType.MARKET),
    ("감성 분석가 (Sentiment Analyst)", AnalystType.SOCIAL),
    ("뉴스 분석가 (News Analyst)", AnalystType.NEWS),
    ("펀더멘털 분석가 (Fundamentals Analyst)", AnalystType.FUNDAMENTALS),
]

CRYPTO_SUFFIXES = ("-USD", "-USDT", "-USDC", "-BTC", "-ETH")


def is_valid_ticker_input(value: str) -> bool:
    """입력된 종목 코드(ticker)가 허용 가능한지 검사한다 (문자 집합 + 길이).

    야후(Yahoo) 심볼이 쓰는 문자를 허용한다. ``GC=F``, ``EURUSD=X`` 같은
    선물/외환용 ``=`` (#980), 지수용 ``^`` 포함. 빈 입력도 허용된다
    (다음 단계에서 SPY로 기본 처리됨).
    """
    v = value.strip()
    return not v or (all(ch.isalnum() or ch in "._-^=" for ch in v) and len(v) <= 32)


def get_ticker() -> str:
    """종목 코드를 입력받는다. 거래소 접미사(suffix)는 그대로 보존한다.

    typer.prompt는 일부 셸에서 ``000404.SH`` 같은 점 접미사를 잘라내므로
    questionary.text를 사용하고, 실행 시작 전에 명백한 오타를 잡을 수 있도록
    심볼 문자 집합을 검증한다.
    """
    ticker = questionary.text(
        f"종목 코드를 입력하세요 (예: {TICKER_INPUT_EXAMPLES}):",
        validate=lambda x: (
            is_valid_ticker_input(x)
            or "올바른 종목 코드를 입력해 주세요 (예: AAPL, 000404.SZ, 0700.HK, GC=F)."
        ),
        style=questionary.Style(
            [
                ("text", "fg:green"),
                ("highlighted", "noinherit"),
            ]
        ),
    ).ask()

    if ticker is None:
        console.print("\n[red]종목 코드가 입력되지 않았습니다. 종료합니다...[/red]")
        exit(1)

    return normalize_ticker_symbol(ticker) if ticker.strip() else "SPY"


def normalize_ticker_symbol(ticker: str) -> str:
    """사용자 입력을 표준(canonical) 야후 심볼로 변환한다 (단일 진실 공급원, single source of truth).

    데이터 계층의 ``normalize_symbol``에 위임해, CLI가 파이프라인에 넘기는
    심볼이 데이터 경로에서 실제로 가격을 조회할 심볼과 정확히 일치하게 한다
    (예: ``BTCUSD`` -> ``BTC-USD``, ``XAUUSD`` -> ``GC=F``). 데이터 계층을
    사용할 수 없으면 단순 대문자 변환으로 대체한다.
    """
    try:
        from tradingagents.dataflows.symbol_utils import normalize_symbol

        return normalize_symbol(ticker)
    except Exception:
        return ticker.strip().upper()


def detect_asset_type(ticker: str) -> AssetType:
    """표준 심볼을 기준으로 자산 유형을 분류한다. 예컨대 BTCUSD와 BTC-USDT가
    모두 암호화폐(crypto)로 인식되게 하여 (#981/#982), 데이터 경로가 실제로
    가져올 대상과 일치시킨다."""
    canonical = normalize_ticker_symbol(ticker)
    if canonical.endswith(CRYPTO_SUFFIXES):
        return AssetType.CRYPTO
    return AssetType.STOCK


def filter_analysts_for_asset_type(
    analysts: list[AnalystType], asset_type: AssetType
) -> list[AnalystType]:
    if asset_type != AssetType.CRYPTO:
        return analysts
    return [
        analyst
        for analyst in analysts
        if analyst != AnalystType.FUNDAMENTALS
    ]


def get_analysis_date() -> str:
    """YYYY-MM-DD 형식의 분석 날짜를 입력받는다."""
    import re
    from datetime import datetime

    def validate_date(date_str: str) -> bool:
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
            return False
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
            return True
        except ValueError:
            return False

    date = questionary.text(
        "분석 날짜를 입력하세요 (YYYY-MM-DD):",
        validate=lambda x: validate_date(x.strip())
        or "YYYY-MM-DD 형식의 올바른 날짜를 입력해 주세요.",
        style=questionary.Style(
            [
                ("text", "fg:green"),
                ("highlighted", "noinherit"),
            ]
        ),
    ).ask()

    if not date:
        console.print("\n[red]날짜가 입력되지 않았습니다. 종료합니다...[/red]")
        exit(1)

    return date.strip()


def select_analysts(asset_type: AssetType = AssetType.STOCK) -> list[AnalystType]:
    """대화형 체크박스(checkbox)로 애널리스트를 선택한다."""
    available_analysts = filter_analysts_for_asset_type(
        [value for _, value in ANALYST_ORDER],
        asset_type,
    )
    choices = questionary.checkbox(
        "[분석가 팀]을 선택하세요:",
        choices=[
            questionary.Choice(display, value=value)
            for display, value in ANALYST_ORDER
            if value in available_analysts
        ],
        instruction="\n- Space로 분석가 선택/해제\n- 'a'로 전체 선택/해제\n- 완료하면 Enter",
        validate=lambda x: len(x) > 0 or "분석가를 최소 한 명은 선택해야 합니다.",
        style=questionary.Style(
            [
                ("checkbox-selected", "fg:green"),
                ("selected", "fg:green noinherit"),
                ("highlighted", "noinherit"),
                ("pointer", "noinherit"),
            ]
        ),
    ).ask()

    if not choices:
        console.print("\n[red]분석가가 선택되지 않았습니다. 종료합니다...[/red]")
        exit(1)

    return choices


def select_research_depth() -> int:
    """대화형 선택으로 리서치 깊이(research depth)를 고른다."""

    # 리서치 깊이 옵션과 대응하는 값(라운드 수) 정의
    DEPTH_OPTIONS = [
        ("얕게(Shallow) - 빠른 리서치, 토론·전략 논의 라운드 최소", 1),
        ("중간(Medium) - 중간 수준의 토론 라운드와 전략 논의", 3),
        ("깊게(Deep) - 종합적인 리서치, 심층 토론과 전략 논의", 5),
    ]

    choice = questionary.select(
        "[리서치 깊이]를 선택하세요:",
        choices=[
            questionary.Choice(display, value=value) for display, value in DEPTH_OPTIONS
        ],
        instruction="\n- 방향키로 이동\n- Enter로 선택",
        style=questionary.Style(
            [
                ("selected", "fg:yellow noinherit"),
                ("highlighted", "fg:yellow noinherit"),
                ("pointer", "fg:yellow noinherit"),
            ]
        ),
    ).ask()

    if choice is None:
        console.print("\n[red]리서치 깊이가 선택되지 않았습니다. 종료합니다...[/red]")
        exit(1)

    return choice


# 주류(mainstream) OpenRouter 채팅 LLM 공급자 네임스페이스(namespace) 목록.
# 전체 최신 모델 목록은 틈새/실험적 릴리스가 대부분이라, 그 대신 이 공급자들의
# 최신 모델을 보여준다. 여기 있는 것은 범용 채팅 공급자들이다. 기업용/특화
# 네임스페이스(nvidia, cohere, amazon, ...)는 최신 모델이 연구/안전성 변형인
# 경우가 많아 후보 목록에서 제외했다. 공급자 이름은 (모델 ID와 달리) 안정적이라
# 이 목록을 손볼 일은 드물다. 여기 없는 공급자도 Custom ID로 접근할 수 있다.
_OPENROUTER_MAINSTREAM = {
    "openai", "anthropic", "google", "deepseek", "qwen", "mistralai",
    "meta-llama", "x-ai", "z-ai", "minimax", "moonshotai",
}


def _fetch_openrouter_models() -> list[tuple[str, str]]:
    """OpenRouter API에서 사용 가능한 모델 목록을 가져온다."""
    import requests
    try:
        resp = requests.get("https://openrouter.ai/api/v1/models", timeout=10)
        resp.raise_for_status()
        models = resp.json().get("data", [])
        # 최신순으로 정렬해 화면에 보이는 상위 N개가 정말 최신 모델이 되게 한다.
        # 현재 API가 이 순서로 반환하긴 하지만, 응답 순서와 무관하게 프롬프트의
        # "latest available" 표기가 유효하도록 명시적으로 정렬한다.
        models.sort(key=lambda m: m.get("created") or 0, reverse=True)
        return [(m.get("name") or m["id"], m["id"]) for m in models]
    except Exception as e:
        console.print(f"\n[yellow]OpenRouter 모델 목록을 가져오지 못했습니다: {e}[/yellow]")
        return []


def _require_text(message: str, hint: str) -> str:
    """필수 값을 입력받는다. 사용자가 취소하면 깔끔하게 종료한다.

    ``questionary.text(...).ask()``는 Ctrl-C/Esc 시 None을 반환한다. 다른
    필수 선택들의 취소-시-종료 동작을 그대로 따라, 취소된 프롬프트가 빈
    모델/배포(deployment) 이름을 반환해 이후 단계에서 실패하는 일이 없게 한다.
    """
    response = questionary.text(
        message,
        validate=lambda x: len(x.strip()) > 0 or hint,
    ).ask()
    if response is None:
        console.print("\n[red]취소되었습니다. 종료합니다...[/red]")
        exit(1)
    return response.strip()


def select_openrouter_model(mode: str) -> str:
    """최신 OpenRouter 모델 중에서 선택하거나 사용자 지정 ID를 입력받는다.

    ``mode``("quick"/"deep")는 프롬프트에 라벨을 붙여, 연속으로 나오는 두 번의
    OpenRouter 선택을 다른 공급자들처럼 구분할 수 있게 한다 (#1000).
    """
    models = _fetch_openrouter_models()  # 최신순
    # 후보 목록이 틈새/실험적 릴리스로 채워지지 않도록 주류 공급자의 최신
    # 모델을 우선한다. 하나도 매칭되지 않으면 전체 목록으로 대체한다.
    mainstream = [
        (name, mid) for name, mid in models
        if not mid.startswith("~")  # 변형/별칭(alias) 중복 경로 건너뜀
        and mid.split("/", 1)[0] in _OPENROUTER_MAINSTREAM
    ]
    top = (mainstream or models)[:5]

    choices = [questionary.Choice(name, value=mid) for name, mid in top]
    choices.append(questionary.Choice("사용자 지정 모델 ID", value="custom"))

    choice = questionary.select(
        f"[{mode.title()}-Thinking] OpenRouter 모델을 선택하세요 (최신 모델 순):",
        choices=choices,
        instruction="\n- 방향키로 이동\n- Enter로 선택",
        style=questionary.Style([
            ("selected", "fg:magenta noinherit"),
            ("highlighted", "fg:magenta noinherit"),
            ("pointer", "fg:magenta noinherit"),
        ]),
    ).ask()

    if choice is None:
        console.print("\n[red]모델이 선택되지 않았습니다. 종료합니다...[/red]")
        exit(1)
    if choice == "custom":
        return _require_text(
            "OpenRouter 모델 ID를 입력하세요 (예: google/gemma-4-26b-a4b-it):",
            "모델 ID를 입력해 주세요.",
        )
    return choice


def _prompt_custom_model_id() -> str:
    """사용자 지정 모델 ID를 직접 입력받는다."""
    return _require_text("모델 ID를 입력하세요:", "모델 ID를 입력해 주세요.")


def _select_model(provider: str, mode: str) -> str:
    """주어진 공급자와 모드(quick/deep)에 맞는 모델을 선택한다."""
    if provider.lower() == "openrouter":
        return select_openrouter_model(mode)

    if provider.lower() == "azure":
        return _require_text(
            f"Azure 배포(deployment) 이름을 입력하세요 ({mode}-thinking):",
            "배포 이름을 입력해 주세요.",
        )

    choice = questionary.select(
        f"[{mode.title()}-Thinking LLM 엔진]을 선택하세요:",
        choices=[
            questionary.Choice(display, value=value)
            for display, value in get_model_options(provider, mode)
        ],
        instruction="\n- 방향키로 이동\n- Enter로 선택",
        style=questionary.Style(
            [
                ("selected", "fg:magenta noinherit"),
                ("highlighted", "fg:magenta noinherit"),
                ("pointer", "fg:magenta noinherit"),
            ]
        ),
    ).ask()

    if choice is None:
        console.print(f"\n[red]{mode}-thinking LLM 엔진이 선택되지 않았습니다. 종료합니다...[/red]")
        exit(1)

    if choice == "custom":
        return _prompt_custom_model_id()

    return choice


def select_shallow_thinking_agent(provider) -> str:
    """대화형 선택으로 얕은 사고(shallow thinking) LLM 엔진을 고른다."""
    return _select_model(provider, "quick")


def select_deep_thinking_agent(provider) -> str:
    """대화형 선택으로 깊은 사고(deep thinking) LLM 엔진을 고른다."""
    return _select_model(provider, "deep")

def _llm_provider_table() -> list[tuple[str, str, str | None]]:
    """지원하는 모든 공급자의 (표시 이름, 공급자 키, 기본 URL) 목록.

    대화형 선택기와 환경변수 기반 설정이 공유하므로, 환경변수로 지정한
    공급자도 메뉴와 동일한 기본 엔드포인트로 해석된다.
    Ollama 사용자는 OLLAMA_BASE_URL로 원격 ollama-serve를 지정할 수 있으며
    (Ollama 생태계 전반의 관례), 설정하지 않으면 localhost 기본값을 쓴다.
    """
    ollama_url = os.environ.get("OLLAMA_BASE_URL") or "http://localhost:11434/v1"
    return [
        ("OpenAI", "openai", "https://api.openai.com/v1"),
        ("Google", "google", None),
        ("Anthropic", "anthropic", "https://api.anthropic.com/"),
        ("xAI", "xai", "https://api.x.ai/v1"),
        ("DeepSeek", "deepseek", "https://api.deepseek.com"),
        ("Qwen", "qwen", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"),
        ("GLM", "glm", "https://open.bigmodel.cn/api/paas/v4/"),
        ("MiniMax", "minimax", "https://api.minimax.io/v1"),
        ("OpenRouter", "openrouter", "https://openrouter.ai/api/v1"),
        ("Mistral", "mistral", "https://api.mistral.ai/v1"),
        ("Kimi (Moonshot)", "kimi", "https://api.moonshot.ai/v1"),
        ("Groq", "groq", "https://api.groq.com/openai/v1"),
        ("NVIDIA NIM", "nvidia", "https://integrate.api.nvidia.com/v1"),
        ("Azure OpenAI", "azure", None),
        ("Amazon Bedrock", "bedrock", None),
        ("Ollama", "ollama", ollama_url),
        ("OpenAI 호환 (vLLM, LM Studio, llama.cpp, 사용자 지정 릴레이)", "openai_compatible", None),
    ]


def provider_default_url(provider_key: str) -> str | None:
    """공급자 키에 대한 기본 백엔드 URL을 반환한다. 알 수 없는 키면 None."""
    key = provider_key.lower()
    for _, pk, url in _llm_provider_table():
        if pk == key:
            return url
    return None


def resolve_backend_url(
    provider: str, menu_url: str | None = None, env_url: str | None = None
) -> str | None:
    """올바른 우선순위로 백엔드 URL을 결정한다.

    명시적 환경변수 오버라이드(``env_url``, ``TRADINGAGENTS_LLM_BACKEND_URL``에서
    ``DEFAULT_CONFIG['backend_url']``를 거쳐 옴)는 공급자를 어떻게 골랐든 —
    대화형이든 환경변수든 — 항상 존중된다 (#978).
    그다음은 메뉴/지역 URL, 마지막이 공급자 기본값 순이다.
    """
    return env_url or menu_url or provider_default_url(provider)


def prompt_openai_compatible_url() -> str:
    """사용자 지정 OpenAI 호환(OpenAI-compatible) 엔드포인트의 기본 URL을 입력받는다."""
    url = questionary.text(
        "OpenAI 호환 기본 URL을 입력하세요 "
        "(예: vLLM은 http://localhost:8000/v1, LM Studio는 http://localhost:1234/v1):",
        validate=lambda x: x.strip().startswith(("http://", "https://"))
        or "http:// 또는 https://로 시작하는 URL을 입력해 주세요.",
    ).ask()
    if not url:
        console.print("\n[red]엔드포인트 URL이 입력되지 않았습니다. 종료합니다...[/red]")
        exit(1)
    return url.strip()


def select_llm_provider() -> tuple[str, str | None]:
    """LLM 공급자와 그 API 엔드포인트를 선택한다."""
    PROVIDERS = _llm_provider_table()

    choice = questionary.select(
        "LLM 제공자를 선택하세요:",
        choices=[
            questionary.Choice(display, value=(provider_key, url))
            for display, provider_key, url in PROVIDERS
        ],
        instruction="\n- 방향키로 이동\n- Enter로 선택",
        style=questionary.Style(
            [
                ("selected", "fg:magenta noinherit"),
                ("highlighted", "fg:magenta noinherit"),
                ("pointer", "fg:magenta noinherit"),
            ]
        ),
    ).ask()

    if choice is None:
        console.print("\n[red]LLM 제공자가 선택되지 않았습니다. 종료합니다...[/red]")
        exit(1)

    provider, url = choice
    return provider, url


def ask_openai_reasoning_effort() -> str:
    """OpenAI 추론 강도(reasoning effort) 수준을 물어본다."""
    choices = [
        questionary.Choice("중간 (기본값)", "medium"),
        questionary.Choice("높음 (더 꼼꼼함)", "high"),
        questionary.Choice("낮음 (더 빠름)", "low"),
    ]
    return questionary.select(
        "추론 강도(Reasoning Effort)를 선택하세요:",
        choices=choices,
        style=questionary.Style([
            ("selected", "fg:cyan noinherit"),
            ("highlighted", "fg:cyan noinherit"),
            ("pointer", "fg:cyan noinherit"),
        ]),
    ).ask()


def ask_anthropic_effort() -> str | None:
    """Anthropic 노력 수준(effort level)을 물어본다.

    Claude 4.5 / 4.6 / 4.7 모델에서 토큰 사용량과 응답의 꼼꼼함을 제어한다.
    API는 "max"도 받지만, 일반적인 선택 범위인 low/medium/high만 노출한다.
    """
    return questionary.select(
        "노력 수준(Effort Level)을 선택하세요:",
        choices=[
            questionary.Choice("높음 (권장)", "high"),
            questionary.Choice("중간 (균형)", "medium"),
            questionary.Choice("낮음 (더 빠르고 저렴)", "low"),
        ],
        style=questionary.Style([
            ("selected", "fg:cyan noinherit"),
            ("highlighted", "fg:cyan noinherit"),
            ("pointer", "fg:cyan noinherit"),
        ]),
    ).ask()


def ask_gemini_thinking_config() -> str | None:
    """Gemini 사고(thinking) 설정을 물어본다.

    thinking_level("high" 또는 "minimal")을 반환한다.
    클라이언트가 모델 시리즈에 맞는 API 파라미터로 매핑한다.
    """
    return questionary.select(
        "사고(Thinking) 모드를 선택하세요:",
        choices=[
            questionary.Choice("사고 활성화 (권장)", "high"),
            questionary.Choice("사고 최소화/비활성화", "minimal"),
        ],
        style=questionary.Style([
            ("selected", "fg:green noinherit"),
            ("highlighted", "fg:green noinherit"),
            ("pointer", "fg:green noinherit"),
        ]),
    ).ask()


def ask_glm_region() -> tuple[str, str]:
    """어느 GLM 플랫폼(국제용 Z.AI vs 중국용 BigModel)을 쓸지 물어본다.

    Zhipu는 같은 GLM 모델을 두 브랜드로 서비스하며 계정이 분리되어 있다.
    키는 서로 호환되지 않는다. (provider_key, backend_url)을 반환한다.
    """
    return questionary.select(
        "GLM 플랫폼을 선택하세요:",
        choices=[
            questionary.Choice(
                "Z.AI — api.z.ai (국제용, ZHIPU_API_KEY 사용)",
                value=("glm", "https://api.z.ai/api/paas/v4/"),
            ),
            questionary.Choice(
                "BigModel — open.bigmodel.cn (중국용, ZHIPU_CN_API_KEY 사용)",
                value=("glm-cn", "https://open.bigmodel.cn/api/paas/v4/"),
            ),
        ],
        style=questionary.Style([
            ("selected", "fg:cyan noinherit"),
            ("highlighted", "fg:cyan noinherit"),
            ("pointer", "fg:cyan noinherit"),
        ]),
    ).ask()


def ask_qwen_region() -> tuple[str, str]:
    """어느 Qwen 지역(국제 vs 중국)을 쓸지 물어본다.

    알리바바 DashScope는 계정이 분리된 두 엔드포인트를 제공한다 —
    한 지역의 키로는 다른 지역에 인증할 수 없다 (#758 수정).
    (provider_key, backend_url)을 반환한다.
    """
    return questionary.select(
        "Qwen 지역을 선택하세요:",
        choices=[
            questionary.Choice(
                "국제 — dashscope-intl.aliyuncs.com (DASHSCOPE_API_KEY 사용)",
                value=("qwen", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"),
            ),
            questionary.Choice(
                "중국 — dashscope.aliyuncs.com (DASHSCOPE_CN_API_KEY 사용)",
                value=("qwen-cn", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            ),
        ],
        style=questionary.Style([
            ("selected", "fg:cyan noinherit"),
            ("highlighted", "fg:cyan noinherit"),
            ("pointer", "fg:cyan noinherit"),
        ]),
    ).ask()


def ask_minimax_region() -> tuple[str, str]:
    """어느 MiniMax 지역(글로벌 vs 중국)을 쓸지 물어본다.

    MiniMax는 계정이 분리된 두 엔드포인트를 제공한다 — 한 지역의 키로는
    다른 지역에 인증할 수 없다. (provider_key, backend_url)을 반환한다.
    """
    return questionary.select(
        "MiniMax 지역을 선택하세요:",
        choices=[
            questionary.Choice(
                "글로벌 — api.minimax.io (MINIMAX_API_KEY 사용)",
                value=("minimax", "https://api.minimax.io/v1"),
            ),
            questionary.Choice(
                "중국 — api.minimaxi.com (MINIMAX_CN_API_KEY 사용)",
                value=("minimax-cn", "https://api.minimaxi.com/v1"),
            ),
        ],
        style=questionary.Style([
            ("selected", "fg:cyan noinherit"),
            ("highlighted", "fg:cyan noinherit"),
            ("pointer", "fg:cyan noinherit"),
        ]),
    ).ask()


def confirm_ollama_endpoint(url: str) -> None:
    """공급자 선택 후 최종 결정된 Ollama 엔드포인트를 보여준다.

    모델 선택 전에 사용자가 알아두면 좋은 세 가지를 표시한다: 실제로 접속할
    URL, 그 출처(`OLLAMA_BASE_URL` vs 기본값), 그리고 ollama-serve가 기대하는
    스킴(scheme)/포트가 URL에 빠져 있을 때의 가벼운 경고. 이 경고는 참고용일
    뿐이다 — 사용자가 일부러 특이한 구성(예: 리버스 프록시 경로)을 쓸 수도
    있으므로 잘못된 형식의 입력을 거부하지는 않는다.
    """
    from_env = os.environ.get("OLLAMA_BASE_URL")
    origin = " (OLLAMA_BASE_URL에서 설정됨)" if from_env and from_env == url else ""
    console.print(f"[green]✓ Ollama 사용 주소: {url}{origin}[/green]")

    if not url.startswith(("http://", "https://")):
        console.print(
            f"[yellow]참고: {url!r}에 스킴(scheme)이 없습니다. "
            f"ollama-serve는 보통 http://<host>:11434/v1 형태의 "
            f"URL을 기대합니다.[/yellow]"
        )
    elif ":11434" not in url and "://localhost" not in url and "://127.0.0.1" not in url:
        # 포트가 ollama-serve 기본값과 다르고 호스트가 로컬이 아닐 때
        # (사용자가 :80으로 프록시하는 경우도 있음) 가벼운 힌트를 준다.
        console.print(
            f"[yellow]참고: {url!r}에 포트 11434가 없습니다. "
            f"원격 ollama-serve가 위에 표시된 포트에서 수신 대기 중인지 "
            f"확인하세요.[/yellow]"
        )


def ensure_api_key(provider: str) -> str | None:
    """`provider`의 API 키가 환경에 준비되어 있는지 확인한다.

    환경변수가 이미 설정돼 있으면 그 값을 그대로 반환한다. 아니면 대화형으로
    입력을 요청하고, python-dotenv의 set_key로 프로젝트 .env 파일에 저장한 뒤
    (필요하면 .env 생성), 현재 프로세스가 인식하도록 os.environ에도 내보낸다.

    키가 필요 없는 공급자(예: ollama)와 표준 매핑에 없는 공급자에 대해서는
    None을 반환한다.
    """
    env_var = get_api_key_env(provider)
    if env_var is None:
        return None  # ollama / 알 수 없는 공급자 — 키 확인 불가

    # 키가 선택 사항인 공급자(범용 OpenAI 호환 / 로컬 서버)는 키가 있으면
    # 읽어 쓰되, 절대 대화형 입력을 강제해서는 안 된다.
    from tradingagents.llm_clients.openai_client import OPENAI_COMPATIBLE_PROVIDERS
    spec = OPENAI_COMPATIBLE_PROVIDERS.get(provider.lower())
    if spec is not None and spec.key_optional:
        return os.environ.get(env_var)

    existing = os.environ.get(env_var)
    if existing:
        return existing

    console.print(
        f"\n[yellow]{env_var} 환경변수가 설정되어 있지 않습니다.[/yellow]"
    )
    key = questionary.password(
        f"{env_var} 값을 붙여넣으세요 (.env 파일에 저장됩니다):",
        style=questionary.Style([
            ("text", "fg:cyan"),
            ("highlighted", "noinherit"),
        ]),
    ).ask()
    if not key:
        console.print(
            f"[red]건너뛰었습니다. {env_var}가 설정될 때까지 API 호출은 실패합니다.[/red]"
        )
        return None

    env_path = find_dotenv(usecwd=True) or str(Path.cwd() / ".env")
    Path(env_path).touch(exist_ok=True)
    set_key(env_path, env_var, key)
    os.environ[env_var] = key
    console.print(f"[green]{env_var}를 {env_path}에 저장했습니다[/green]")
    return key


def ask_output_language() -> str:
    """보고서 출력 언어를 물어본다."""
    choice = questionary.select(
        "출력 언어를 선택하세요:",
        choices=[
            # 한글화 포크: Korean을 기본값으로 맨 위에 배치 (원본은 English가 기본)
            questionary.Choice("Korean (한국어, 기본값)", "Korean"),
            questionary.Choice("English", "English"),
            questionary.Choice("Chinese (中文)", "Chinese"),
            questionary.Choice("Japanese (日本語)", "Japanese"),
            questionary.Choice("Hindi (हिन्दी)", "Hindi"),
            questionary.Choice("Spanish (Español)", "Spanish"),
            questionary.Choice("Portuguese (Português)", "Portuguese"),
            questionary.Choice("French (Français)", "French"),
            questionary.Choice("German (Deutsch)", "German"),
            questionary.Choice("Arabic (العربية)", "Arabic"),
            questionary.Choice("Russian (Русский)", "Russian"),
            questionary.Choice("사용자 지정 언어", "custom"),
        ],
        style=questionary.Style([
            ("selected", "fg:yellow noinherit"),
            ("highlighted", "fg:yellow noinherit"),
            ("pointer", "fg:yellow noinherit"),
        ]),
    ).ask()

    # 출력 언어는 합리적인 기본값이 있으므로, 취소해도 (필수인 모델/공급자
    # 질문들과 달리) 실행을 종료하지 않고 Korean(이 포크의 기본값)으로 대체한다.
    if choice is None:
        return "Korean"
    if choice == "custom":
        return (questionary.text(
            "언어 이름을 입력하세요 (예: Turkish, Vietnamese, Thai, Indonesian):",
            validate=lambda x: len(x.strip()) > 0 or "언어 이름을 입력해 주세요.",
        ).ask() or "").strip() or "Korean"

    return choice
