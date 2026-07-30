# =============================================================================
# [모듈 개요 - 초보자용]
# 이 파일은 CLI의 모델 선택 화면과 모델 이름 검증에 함께 쓰이는
# "공용 모델 카탈로그"입니다. 프로바이더(provider)별 x 모드(quick/deep)별로
# (표시 이름, 실제 모델 ID) 쌍의 목록을 정의합니다.
# CLI는 get_model_options()로 선택지 목록을 가져오고,
# validators.py는 get_known_models()로 "알려진 모델 ID 집합"을 만들어
# 오타 검증(경고 표시)에 사용합니다.
# =============================================================================
"""CLI 선택 화면과 검증에 함께 쓰이는 공용 모델 카탈로그."""

from __future__ import annotations

ModelOption = tuple[str, str]
ProviderModeOptions = dict[str, dict[str, list[ModelOption]]]

# 모델 수가 많거나 자주 바뀌는 프로바이더: 금방 낡아버릴 목록 대신
# "Custom model ID"(직접 입력)만 제공한다.
_CUSTOM_ONLY: dict[str, list[ModelOption]] = {
    "quick": [("Custom model ID", "custom")],
    "deep": [("Custom model ID", "custom")],
}


# Z.AI(국제)와 BigModel(중국)을 통한 GLM 공용 모델 목록.
# 출처: docs.z.ai (GLM Coding Plan 지원 모델 + LLM 가이드).
# GLM 4.7 이상 항목은 모두 thinking={"type":"enabled"}로 사고 모드(thinking mode)를 지원한다.
_GLM_MODELS: dict[str, list[ModelOption]] = {
    "quick": [
        ("GLM-5-Turbo - Fast, switchable thinking modes", "glm-5-turbo"),
        ("GLM-4.7 - Previous-gen flagship", "glm-4.7"),
        ("GLM-4.5-Air - Lightweight, cost-efficient", "glm-4.5-air"),
        ("Custom model ID", "custom"),
    ],
    "deep": [
        ("GLM-5.2 - Latest flagship, 1M ctx", "glm-5.2"),
        ("GLM-5.1 - 745B, 200K ctx", "glm-5.1"),
        ("GLM-5 - Flagship, 204K ctx", "glm-5"),
        ("GLM-4.7 - Previous-gen flagship", "glm-4.7"),
        ("Custom model ID", "custom"),
    ],
}


# Qwen의 글로벌(dashscope-intl) / 중국(dashscope) 엔드포인트 공용 모델 목록.
# 출처: modelstudio.console.alibabacloud.com (Featured Models — Flagship + Cost-optimized).
#
# 드롭다운에는 버전이 명시된 ID만 노출한다. 버전 없는 별칭(alias)인
# qwen-plus, qwen-flash는 Alibaba 문서상 자동 업그레이드되는 포인터
# ("backbone, latest, and snapshot ... have been upgraded to the Qwen3
# series")이므로, Alibaba가 뒷단 모델을 교체하면 동작이 달라진다.
# 특정 세대를 원하는 사용자는 명시적으로 선택하고, 정말 자동 최신을
# 원하는 사용자는 "Custom model ID"로 별칭을 직접 입력하면 된다.
_QWEN_MODELS: dict[str, list[ModelOption]] = {
    "quick": [
        ("Qwen 3.7 Plus - Latest, balanced speed/cost", "qwen3.7-plus"),
        ("Qwen 3.6 Plus - Previous-gen balanced", "qwen3.6-plus"),
        ("Custom model ID", "custom"),
    ],
    "deep": [
        ("Qwen 3.7 Max - Latest flagship, most intelligent, 1M ctx", "qwen3.7-max"),
        ("Qwen 3.6 Max - Previous-gen flagship", "qwen3.6-max"),
        ("Qwen 3.7 Plus - Balanced alternative", "qwen3.7-plus"),
        ("Custom model ID", "custom"),
    ],
}


# MiniMax의 글로벌 / 중국 엔드포인트 공용 모델 목록 (모델 ID는 동일).
# 공식 전체 라인업 출처: platform.minimax.io/docs/api-reference/text-openai-api.
# M3는 100만(1M) 토큰 컨텍스트 윈도우를 가지며, M2.x 라인은 204,800 토큰이다.
_MINIMAX_MODELS: dict[str, list[ModelOption]] = {
    "quick": [
        ("MiniMax-M3 - Latest, 1M ctx, native multimodal", "MiniMax-M3"),
        ("MiniMax-M2.7-highspeed - Fast M2.7, 204K ctx, ~100 TPS", "MiniMax-M2.7-highspeed"),
        ("MiniMax-M2.5-highspeed - Previous-gen highspeed, 204K ctx", "MiniMax-M2.5-highspeed"),
        ("Custom model ID", "custom"),
    ],
    "deep": [
        ("MiniMax-M3 - Latest flagship, 1M ctx, multimodal coding/agent", "MiniMax-M3"),
        ("MiniMax-M2.7 - Previous flagship, 204K ctx", "MiniMax-M2.7"),
        ("MiniMax-M2.7-highspeed - Same quality as M2.7, ~100 TPS", "MiniMax-M2.7-highspeed"),
        ("MiniMax-M2.5 - Earlier flagship, 204K ctx", "MiniMax-M2.5"),
        ("Custom model ID", "custom"),
    ],
}


MODEL_OPTIONS: ProviderModeOptions = {
    "openai": {
        "quick": [
            ("GPT-5.4 Mini - Fast, strong coding and tool use", "gpt-5.4-mini"),
            ("GPT-5.4 Nano - Cheapest, high-volume tasks", "gpt-5.4-nano"),
            ("GPT-5.5 - Latest frontier, 1M context", "gpt-5.5"),
        ],
        "deep": [
            ("GPT-5.5 - Latest frontier, 1M context", "gpt-5.5"),
            ("GPT-5.4 - Previous-gen frontier, 1M context, cost-effective", "gpt-5.4"),
            ("GPT-5.2 - Strong reasoning, cost-effective", "gpt-5.2"),
            ("GPT-5.5 Pro - Most capable, expensive ($30/$180 per 1M tokens)", "gpt-5.5-pro"),
        ],
    },
    "anthropic": {
        "quick": [
            ("Claude Sonnet 5 - Best speed and intelligence balance", "claude-sonnet-5"),
            ("Claude Haiku 4.5 - Fastest with near-frontier intelligence", "claude-haiku-4-5"),
        ],
        "deep": [
            ("Claude Fable 5 - Most capable, long-running agents", "claude-fable-5"),
            ("Claude Opus 4.8 - Frontier agentic coding and reasoning", "claude-opus-4-8"),
            ("Claude Sonnet 5 - Near-frontier intelligence at Sonnet cost", "claude-sonnet-5"),
            ("Claude Opus 4.7 - Previous frontier, long-running agents", "claude-opus-4-7"),
        ],
    },
    "google": {
        "quick": [
            ("Gemini 3.5 Flash - Latest, frontier agentic + coding (GA)", "gemini-3.5-flash"),
            ("Gemini 3.1 Flash Lite - Most cost-efficient", "gemini-3.1-flash-lite"),
        ],
        "deep": [
            ("Gemini 3.1 Pro - Reasoning-first, complex workflows (preview)", "gemini-3.1-pro-preview"),
            ("Gemini 3.5 Flash - Latest GA, strong agentic + coding", "gemini-3.5-flash"),
        ],
    },
    "xai": {
        "quick": [
            ("Grok 4.3 - Latest flagship, fast with built-in reasoning", "grok-4.3"),
            ("Grok 4.20 (Non-Reasoning) - Speed-optimized", "grok-4.20-0309-non-reasoning"),
            ("Grok Build 0.1 - Coding-specialized, 256K ctx", "grok-build-0.1"),
        ],
        "deep": [
            ("Grok 4.3 - Latest flagship, built-in reasoning, 1M ctx", "grok-4.3"),
            ("Grok 4.20 (Reasoning) - Previous-gen reasoning", "grok-4.20-0309-reasoning"),
            ("Grok 4.20 Multi-Agent - Multi-agent reasoning", "grok-4.20-multi-agent-0309"),
        ],
    },
    # DeepSeek: deepseek-chat / deepseek-reasoner 별칭은 폐기 예정(deprecated,
    # 2026-07-24)이며 현재 V4 Flash로 매핑된다; V4 ID를 직접 노출한다. V4 Flash는
    # 비사고(non-thinking)와 사고(thinking) 모드를 모두 제공한다
    # (reasoning_content 왕복 처리는 DeepSeekChatOpenAI 클라이언트가 담당).
    "deepseek": {
        "quick": [
            ("DeepSeek V4 Flash - Latest fast model, thinking + non-thinking", "deepseek-v4-flash"),
            ("Custom model ID", "custom"),
        ],
        "deep": [
            ("DeepSeek V4 Pro - Latest flagship", "deepseek-v4-pro"),
            ("DeepSeek V4 Flash - Fast, supports thinking", "deepseek-v4-flash"),
            ("Custom model ID", "custom"),
        ],
    },
    # Qwen: 글로벌(dashscope-intl)과 중국(dashscope) 엔드포인트에서 모델 ID가
    # 같으므로, 두 프로바이더 키가 하나의 모델 목록을 공유한다.
    "qwen": _QWEN_MODELS,
    "qwen-cn": _QWEN_MODELS,
    # GLM: Z.AI(국제)와 BigModel(중국)이 같은 모델 ID를 호스팅하므로,
    # 두 프로바이더 키가 하나의 모델 목록을 공유한다.
    "glm": _GLM_MODELS,
    "glm-cn": _GLM_MODELS,
    # MiniMax: 글로벌(.io)과 중국(.com) 리전에서 모델 ID가 같으므로,
    # 두 프로바이더 키가 하나의 모델 목록을 공유한다.
    "minimax": _MINIMAX_MODELS,
    "minimax-cn": _MINIMAX_MODELS,
    # OpenRouter: 동적으로 가져온다. Azure: 배포된 어떤 모델 이름이든 허용.
    # Ollama 표시 라벨에는 의도적으로 "local" 표기를 넣지 않았다 —
    # 엔드포인트가 OLLAMA_BASE_URL로 설정 가능해졌으므로, 사용자가
    # ollama-serve를 localhost에서 돌리든 원격 호스트를 쓰든 같은 라벨이
    # 적용된다. 실제로 결정된 엔드포인트는 프로바이더 선택 직후
    # cli.utils.confirm_ollama_endpoint()가 별도로 보여준다.
    # "Custom model ID"를 통해 사용자는 제안된 세 가지 기본값 외에
    # `ollama pull`로 받아 둔 어떤 모델이든 선택할 수 있다.
    "ollama": {
        "quick": [
            ("Qwen3:latest (8B)", "qwen3:latest"),
            ("GPT-OSS:latest (20B)", "gpt-oss:latest"),
            ("GLM-4.7-Flash:latest (30B)", "glm-4.7-flash:latest"),
            ("Custom model ID", "custom"),
        ],
        "deep": [
            ("GLM-4.7-Flash:latest (30B)", "glm-4.7-flash:latest"),
            ("GPT-OSS:latest (20B)", "gpt-oss:latest"),
            ("Qwen3:latest (8B)", "qwen3:latest"),
            ("Custom model ID", "custom"),
        ],
    },
    # 범용 OpenAI 호환(OpenAI-compatible) 엔드포인트: 모델은 사용자의 서버가
    # 제공하는 것이므로 "Custom model ID"만 제공한다.
    "openai_compatible": _CUSTOM_ONLY,
    # 많은 (그리고 자주 바뀌는) 모델을 제공하는 호스팅형 OpenAI 호환
    # 프로바이더들 — 금방 낡아버릴 목록 대신 "Custom model ID"를 제공한다.
    # 엔드포인트와 키는 프로바이더 설정이 연결하고, 모델은 사용자가 자기
    # 계정에서 접근 가능한 것을 고른다.
    "mistral": _CUSTOM_ONLY,
    "kimi": _CUSTOM_ONLY,
    "groq": _CUSTOM_ONLY,
    "nvidia": _CUSTOM_ONLY,
    # Bedrock 모델 ID / 교차 리전 추론 프로필(cross-region inference profile) ID는 사용자가 지정한다.
    "bedrock": _CUSTOM_ONLY,
}


def get_model_options(provider: str, mode: str) -> list[ModelOption]:
    """프로바이더와 선택 모드에 대한 공용 모델 옵션 목록을 반환한다."""
    return MODEL_OPTIONS[provider.lower()][mode]


def get_known_models() -> dict[str, list[str]]:
    """공용 CLI 카탈로그로부터 알려진 모델 이름 목록을 만든다.

    [초보자용 설명] MODEL_OPTIONS의 (표시 이름, 모델 ID) 쌍들에서 모델 ID만
    추려 프로바이더별로 정렬된 리스트를 만든다. validators.py가 이 결과로
    "알려진 모델인지" 검사한다.
    """
    return {
        provider: sorted(
            {
                value
                for options in mode_options.values()
                for _, value in options
            }
        )
        for provider, mode_options in MODEL_OPTIONS.items()
    }
