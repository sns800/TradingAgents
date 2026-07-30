# =============================================================================
# [모듈 개요 - 초보자용]
# 이 파일은 팩토리 패턴(factory pattern)의 구현체입니다. 설정 파일에 적힌
# 프로바이더 이름("anthropic", "openai" 등)을 받아, 그에 맞는 LLM 클라이언트
# 객체를 대신 만들어 주는 단일 창구 역할을 합니다.
# 호출하는 쪽(트레이딩 그래프/에이전트 초기화 코드)은 각 클라이언트 클래스를
# 몰라도 create_llm_client("anthropic", ...)처럼 이름만 넘기면 됩니다.
# =============================================================================

from .base_client import BaseLLMClient


def create_llm_client(
    provider: str,
    model: str,
    base_url: str | None = None,
    **kwargs,
) -> BaseLLMClient:
    """지정된 프로바이더(provider)에 맞는 LLM 클라이언트를 생성한다.

    프로바이더 모듈은 지연 import(lazy import)된다. 그래서 이 팩토리를
    단순히 import하는 것만으로는 (예: 테스트 수집 중) 무거운 LLM SDK를
    불러오거나 API 키 부재로 실패하는 일이 없다.

    Args:
        provider: LLM 프로바이더 이름
        model: 모델 이름/식별자
        base_url: API 엔드포인트의 base URL (선택)
        **kwargs: 프로바이더별 추가 인자

    Returns:
        설정이 완료된 BaseLLMClient 인스턴스

    Raises:
        ValueError: 지원하지 않는 프로바이더인 경우
    """
    provider_lower = provider.lower()

    # 네이티브(비 OpenAI) API를 먼저 매칭해서, 이들의 문자열 검사가 OpenAI
    # 클라이언트를 import하지 않게 한다. 나머지는 모두 OpenAI 호환
    # (OpenAI-compatible)이며 프로바이더 레지스트리(단일 진실 공급원,
    # single source of truth)를 통해 라우팅된다.
    if provider_lower == "anthropic":
        from .anthropic_client import AnthropicClient
        return AnthropicClient(model, base_url, **kwargs)

    if provider_lower == "google":
        from .google_client import GoogleClient
        return GoogleClient(model, base_url, **kwargs)

    if provider_lower == "azure":
        from .azure_client import AzureOpenAIClient
        return AzureOpenAIClient(model, base_url, **kwargs)

    if provider_lower == "bedrock":
        from .bedrock_client import BedrockClient
        return BedrockClient(model, base_url, **kwargs)

    # [초보자용 설명] 위의 네 가지 네이티브 API에 해당하지 않으면,
    # OpenAI 호환 레지스트리에 등록된 프로바이더인지 확인해서
    # 하나의 OpenAIClient로 처리한다 (xAI, DeepSeek, Ollama 등).
    from .openai_client import OpenAIClient, is_openai_compatible
    if is_openai_compatible(provider_lower):
        return OpenAIClient(model, base_url, provider=provider_lower, **kwargs)

    raise ValueError(f"Unsupported LLM provider: {provider}")
