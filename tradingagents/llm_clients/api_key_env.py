# =============================================================================
# [모듈 개요 - 초보자용]
# 이 파일은 "어떤 프로바이더(provider)가 어떤 환경변수(environment variable)에서
# API 키를 읽는가"를 한 곳에 모아 둔 매핑 테이블입니다.
# CLI의 대화형 키 입력 흐름(cli/utils.ensure_api_key)과 openai_client.py 등이
# 이 테이블을 참조해 "키가 필요한가? 어느 환경변수인가?"를 판단합니다.
# 새 프로바이더를 추가할 때는 여기에도 등록해야 CLI가 자동으로 키를 물어봅니다.
# =============================================================================
"""프로바이더 -> API 키 환경변수의 표준(canonical) 매핑.

각 LLM 프로바이더의 API 키가 어떤 환경변수에 들어 있는지를 정의하는
단일 진실 공급원(single source of truth)이다. CLI의 대화형 키 입력
프롬프트(cli/utils.ensure_api_key)와, "이 프로바이더는 키가 필요한가,
필요하다면 어떤 환경변수인가?"를 물어야 하는 모든 코드가 사용한다.

새 프로바이더를 추가할 때는 여기에 환경변수를 등록해야 첫 API 호출에서
실패하는 대신 CLI 흐름이 자동으로 키를 물어본다.
"""

from __future__ import annotations

PROVIDER_API_KEY_ENV: dict[str, str | None] = {
    "openai":     "OPENAI_API_KEY",
    "anthropic":  "ANTHROPIC_API_KEY",
    "google":     "GOOGLE_API_KEY",
    "azure":      "AZURE_OPENAI_API_KEY",
    # Bedrock은 단일 키 환경변수가 아니라 AWS 자격 증명 체인(credential chain)으로 인증한다.
    "bedrock":    None,
    "xai":        "XAI_API_KEY",
    "deepseek":   "DEEPSEEK_API_KEY",
    # 이중 리전(dual-region) 프로바이더는 리전별로 계정이 따로 있으며,
    # 국제(international) 엔드포인트와 중국(China) 엔드포인트 간에
    # 키를 서로 바꿔 쓸 수 없다.
    "qwen":       "DASHSCOPE_API_KEY",
    "qwen-cn":    "DASHSCOPE_CN_API_KEY",
    "glm":        "ZHIPU_API_KEY",
    "glm-cn":     "ZHIPU_CN_API_KEY",
    "minimax":    "MINIMAX_API_KEY",
    "minimax-cn": "MINIMAX_CN_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    # 그 밖의 호스팅형 OpenAI 호환(OpenAI-compatible) 프로바이더 (모델은 사용자가 지정).
    # kimi -> Moonshot AI; nvidia -> NVIDIA NIM.
    "mistral":    "MISTRAL_API_KEY",
    "kimi":       "MOONSHOT_API_KEY",
    "groq":       "GROQ_API_KEY",
    "nvidia":     "NVIDIA_API_KEY",
    # 로컬 런타임(local runtime)은 인증하지 않는다.
    "ollama":     None,
    # 범용 OpenAI 호환 엔드포인트: 이 환경변수가 설정되어 있으면 클라이언트가
    # 읽어 사용하지만(키가 필요한 릴레이용), 프로바이더 레지스트리에서는
    # 키 선택(key-optional)으로 표시되어 CLI가 입력을 강제하지 않으며
    # 키 없는 로컬 서버도 그대로 동작한다.
    "openai_compatible": "OPENAI_COMPATIBLE_API_KEY",
}


def get_api_key_env(provider: str) -> str | None:
    """`provider`의 API 키 환경변수 이름을 반환하고, 해당 없으면 None을 반환한다.

    알 수 없는 프로바이더도 None을 반환한다 — 호출자는 이를 "키가 필요
    없다"가 아니라 "키 확인이 불가능하다"로 해석해야 한다.
    """
    return PROVIDER_API_KEY_ENV.get(provider.lower())
