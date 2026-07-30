# =============================================================================
# [모듈 개요 - 초보자용]
# 이 파일은 "이 프로바이더(provider)에 이 모델 이름이 유효한가?"를 검사하는
# 간단한 검증기입니다. model_catalog.py의 공용 카탈로그에서 알려진 모델
# 목록을 만들어 두고, 각 클라이언트의 validate_model()이 이를 참조합니다.
# 검증 실패는 오류가 아니라 경고(warning)로만 이어집니다 — 실행은 계속됩니다.
# =============================================================================
"""프로바이더별 모델 이름 검증기(validator)."""

from .model_catalog import get_known_models

# 모델 이름이 사용자 정의인 프로바이더들 (로컬 서버, 릴레이, 많은 모델을
# 제공하는 호스팅형 OpenAI 호환 엔드포인트). 어떤 모델 문자열이든 경고 없이
# 허용된다.
_ANY_MODEL_PROVIDERS = (
    "ollama", "openrouter", "openai_compatible",
    "mistral", "kimi", "groq", "nvidia", "bedrock",
)

VALID_MODELS = {
    provider: models
    for provider, models in get_known_models().items()
    if provider not in _ANY_MODEL_PROVIDERS
}


def validate_model(provider: str, model: str) -> bool:
    """주어진 프로바이더에 대해 모델 이름이 유효한지 확인한다.

    ollama, openrouter, openai_compatible의 경우 — 어떤 모델이든 허용된다.
    """
    provider_lower = provider.lower()

    if provider_lower in _ANY_MODEL_PROVIDERS:
        return True

    # [초보자용 설명] 카탈로그에 아예 없는 프로바이더는 검사할 기준이 없으므로
    # 유효한 것으로 간주한다 (거짓 경고 방지).
    if provider_lower not in VALID_MODELS:
        return True

    return model in VALID_MODELS[provider_lower]
