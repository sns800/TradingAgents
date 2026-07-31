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
    ("Market Analyst", AnalystType.MARKET),
    ("Sentiment Analyst", AnalystType.SOCIAL),
    ("News Analyst", AnalystType.NEWS),
    ("Fundamentals Analyst", AnalystType.FUNDAMENTALS),
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
        f"Enter ticker symbol (e.g. {TICKER_INPUT_EXAMPLES}):",
        validate=lambda x: (
            is_valid_ticker_input(x)
            or "Please enter a valid ticker symbol, e.g. AAPL, 000404.SZ, 0700.HK, GC=F."
        ),
        style=questionary.Style(
            [
                ("text", "fg:green"),
                ("highlighted", "noinherit"),
            ]
        ),
    ).ask()

    if ticker is None:
        console.print("\n[red]No ticker symbol provided. Exiting...[/red]")
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
        "Enter the analysis date (YYYY-MM-DD):",
        validate=lambda x: validate_date(x.strip())
        or "Please enter a valid date in YYYY-MM-DD format.",
        style=questionary.Style(
            [
                ("text", "fg:green"),
                ("highlighted", "noinherit"),
            ]
        ),
    ).ask()

    if not date:
        console.print("\n[red]No date provided. Exiting...[/red]")
        exit(1)

    return date.strip()


def select_analysts(asset_type: AssetType = AssetType.STOCK) -> list[AnalystType]:
    """대화형 체크박스(checkbox)로 애널리스트를 선택한다."""
    available_analysts = filter_analysts_for_asset_type(
        [value for _, value in ANALYST_ORDER],
        asset_type,
    )
    choices = questionary.checkbox(
        "Select Your [Analysts Team]:",
        choices=[
            questionary.Choice(display, value=value)
            for display, value in ANALYST_ORDER
            if value in available_analysts
        ],
        instruction="\n- Press Space to select/unselect analysts\n- Press 'a' to select/unselect all\n- Press Enter when done",
        validate=lambda x: len(x) > 0 or "You must select at least one analyst.",
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
        console.print("\n[red]No analysts selected. Exiting...[/red]")
        exit(1)

    return choices


def select_research_depth() -> int:
    """대화형 선택으로 리서치 깊이(research depth)를 고른다."""

    # 리서치 깊이 옵션과 대응하는 값(라운드 수) 정의
    DEPTH_OPTIONS = [
        ("Shallow - Quick research, few debate and strategy discussion rounds", 1),
        ("Medium - Middle ground, moderate debate rounds and strategy discussion", 3),
        ("Deep - Comprehensive research, in depth debate and strategy discussion", 5),
    ]

    choice = questionary.select(
        "Select Your [Research Depth]:",
        choices=[
            questionary.Choice(display, value=value) for display, value in DEPTH_OPTIONS
        ],
        instruction="\n- Use arrow keys to navigate\n- Press Enter to select",
        style=questionary.Style(
            [
                ("selected", "fg:yellow noinherit"),
                ("highlighted", "fg:yellow noinherit"),
                ("pointer", "fg:yellow noinherit"),
            ]
        ),
    ).ask()

    if choice is None:
        console.print("\n[red]No research depth selected. Exiting...[/red]")
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
        console.print(f"\n[yellow]Could not fetch OpenRouter models: {e}[/yellow]")
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
        console.print("\n[red]Cancelled. Exiting...[/red]")
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
    choices.append(questionary.Choice("Custom model ID", value="custom"))

    choice = questionary.select(
        f"Select Your [{mode.title()}-Thinking] OpenRouter Model (latest available):",
        choices=choices,
        instruction="\n- Use arrow keys to navigate\n- Press Enter to select",
        style=questionary.Style([
            ("selected", "fg:magenta noinherit"),
            ("highlighted", "fg:magenta noinherit"),
            ("pointer", "fg:magenta noinherit"),
        ]),
    ).ask()

    if choice is None:
        console.print("\n[red]No model selected. Exiting...[/red]")
        exit(1)
    if choice == "custom":
        return _require_text(
            "Enter OpenRouter model ID (e.g. google/gemma-4-26b-a4b-it):",
            "Please enter a model ID.",
        )
    return choice


def _prompt_custom_model_id() -> str:
    """사용자 지정 모델 ID를 직접 입력받는다."""
    return _require_text("Enter model ID:", "Please enter a model ID.")


def _select_model(provider: str, mode: str) -> str:
    """주어진 공급자와 모드(quick/deep)에 맞는 모델을 선택한다."""
    if provider.lower() == "openrouter":
        return select_openrouter_model(mode)

    if provider.lower() == "azure":
        return _require_text(
            f"Enter Azure deployment name ({mode}-thinking):",
            "Please enter a deployment name.",
        )

    choice = questionary.select(
        f"Select Your [{mode.title()}-Thinking LLM Engine]:",
        choices=[
            questionary.Choice(display, value=value)
            for display, value in get_model_options(provider, mode)
        ],
        instruction="\n- Use arrow keys to navigate\n- Press Enter to select",
        style=questionary.Style(
            [
                ("selected", "fg:magenta noinherit"),
                ("highlighted", "fg:magenta noinherit"),
                ("pointer", "fg:magenta noinherit"),
            ]
        ),
    ).ask()

    if choice is None:
        console.print(f"\n[red]No {mode} thinking llm engine selected. Exiting...[/red]")
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
        ("OpenAI-compatible (vLLM, LM Studio, llama.cpp, custom relay)", "openai_compatible", None),
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
        "Enter the OpenAI-compatible base URL "
        "(e.g. http://localhost:8000/v1 for vLLM, http://localhost:1234/v1 for LM Studio):",
        validate=lambda x: x.strip().startswith(("http://", "https://"))
        or "Enter a URL starting with http:// or https://",
    ).ask()
    if not url:
        console.print("\n[red]No endpoint URL provided. Exiting...[/red]")
        exit(1)
    return url.strip()


def select_llm_provider() -> tuple[str, str | None]:
    """LLM 공급자와 그 API 엔드포인트를 선택한다."""
    PROVIDERS = _llm_provider_table()

    choice = questionary.select(
        "Select your LLM Provider:",
        choices=[
            questionary.Choice(display, value=(provider_key, url))
            for display, provider_key, url in PROVIDERS
        ],
        instruction="\n- Use arrow keys to navigate\n- Press Enter to select",
        style=questionary.Style(
            [
                ("selected", "fg:magenta noinherit"),
                ("highlighted", "fg:magenta noinherit"),
                ("pointer", "fg:magenta noinherit"),
            ]
        ),
    ).ask()

    if choice is None:
        console.print("\n[red]No LLM provider selected. Exiting...[/red]")
        exit(1)

    provider, url = choice
    return provider, url


def ask_openai_reasoning_effort() -> str:
    """OpenAI 추론 강도(reasoning effort) 수준을 물어본다."""
    choices = [
        questionary.Choice("Medium (Default)", "medium"),
        questionary.Choice("High (More thorough)", "high"),
        questionary.Choice("Low (Faster)", "low"),
    ]
    return questionary.select(
        "Select Reasoning Effort:",
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
        "Select Effort Level:",
        choices=[
            questionary.Choice("High (recommended)", "high"),
            questionary.Choice("Medium (balanced)", "medium"),
            questionary.Choice("Low (faster, cheaper)", "low"),
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
        "Select Thinking Mode:",
        choices=[
            questionary.Choice("Enable Thinking (recommended)", "high"),
            questionary.Choice("Minimal/Disable Thinking", "minimal"),
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
        "Select GLM platform:",
        choices=[
            questionary.Choice(
                "Z.AI — api.z.ai (international, uses ZHIPU_API_KEY)",
                value=("glm", "https://api.z.ai/api/paas/v4/"),
            ),
            questionary.Choice(
                "BigModel — open.bigmodel.cn (China, uses ZHIPU_CN_API_KEY)",
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
        "Select Qwen region:",
        choices=[
            questionary.Choice(
                "International — dashscope-intl.aliyuncs.com (uses DASHSCOPE_API_KEY)",
                value=("qwen", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"),
            ),
            questionary.Choice(
                "China — dashscope.aliyuncs.com (uses DASHSCOPE_CN_API_KEY)",
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
        "Select MiniMax region:",
        choices=[
            questionary.Choice(
                "Global — api.minimax.io (uses MINIMAX_API_KEY)",
                value=("minimax", "https://api.minimax.io/v1"),
            ),
            questionary.Choice(
                "China — api.minimaxi.com (uses MINIMAX_CN_API_KEY)",
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
    origin = " (from OLLAMA_BASE_URL)" if from_env and from_env == url else ""
    console.print(f"[green]✓ Using Ollama at {url}{origin}[/green]")

    if not url.startswith(("http://", "https://")):
        console.print(
            f"[yellow]Note: {url!r} is missing a scheme. "
            f"Ollama-serve typically expects a URL like "
            f"http://<host>:11434/v1.[/yellow]"
        )
    elif ":11434" not in url and "://localhost" not in url and "://127.0.0.1" not in url:
        # 포트가 ollama-serve 기본값과 다르고 호스트가 로컬이 아닐 때
        # (사용자가 :80으로 프록시하는 경우도 있음) 가벼운 힌트를 준다.
        console.print(
            f"[yellow]Note: {url!r} doesn't include port 11434. "
            f"Make sure your remote ollama-serve listens on the port "
            f"shown above.[/yellow]"
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
        f"\n[yellow]{env_var} is not set in your environment.[/yellow]"
    )
    key = questionary.password(
        f"Paste your {env_var} (will be saved to .env):",
        style=questionary.Style([
            ("text", "fg:cyan"),
            ("highlighted", "noinherit"),
        ]),
    ).ask()
    if not key:
        console.print(
            f"[red]Skipped. API calls will fail until {env_var} is set.[/red]"
        )
        return None

    env_path = find_dotenv(usecwd=True) or str(Path.cwd() / ".env")
    Path(env_path).touch(exist_ok=True)
    set_key(env_path, env_var, key)
    os.environ[env_var] = key
    console.print(f"[green]Saved {env_var} to {env_path}[/green]")
    return key


def ask_output_language() -> str:
    """보고서 출력 언어를 물어본다."""
    choice = questionary.select(
        "Select Output Language:",
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
            questionary.Choice("Custom language", "custom"),
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
            "Enter language name (e.g. Turkish, Vietnamese, Thai, Indonesian):",
            validate=lambda x: len(x.strip()) > 0 or "Please enter a language name.",
        ).ask() or "").strip() or "Korean"

    return choice
