"""[모듈 개요] OpenAI 호환 제공자 레지스트리(registry)를 검증하는 테스트.

레지스트리는 이 제품군의 단일 진실 공급원(single source of truth)이다.
각 제공자의 확정된 설정(기본 URL, 서브클래스, 인증, Responses API)을 지켜서
이후의 수정이 어느 하나를 조용히 깨뜨리지 못하게 한다.
"""
import pytest

from tradingagents.llm_clients.openai_client import (
    OPENAI_COMPATIBLE_PROVIDERS,
    DeepSeekChatOpenAI,
    MinimaxChatOpenAI,
    NormalizedChatOpenAI,
    is_openai_compatible,
)


@pytest.mark.unit
def test_registry_membership():
    """레지스트리에 포함/제외되어야 할 제공자 목록이 올바른지 검증하는 테스트."""
    assert is_openai_compatible("openai")
    assert is_openai_compatible("openai_compatible")  # 범용(generic) 엔드포인트
    # 자체(native) API를 쓰는 클라이언트는 의도적으로 레지스트리에 넣지 않는다
    assert not is_openai_compatible("anthropic")
    assert not is_openai_compatible("google")
    assert not is_openai_compatible("azure")


@pytest.mark.unit
@pytest.mark.parametrize("provider,base_url,chat_class,responses", [
    ("openai", None, NormalizedChatOpenAI, True),
    ("xai", "https://api.x.ai/v1", NormalizedChatOpenAI, False),
    ("deepseek", "https://api.deepseek.com", DeepSeekChatOpenAI, False),
    ("qwen", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", NormalizedChatOpenAI, False),
    ("qwen-cn", "https://dashscope.aliyuncs.com/compatible-mode/v1", NormalizedChatOpenAI, False),
    ("glm", "https://api.z.ai/api/paas/v4/", NormalizedChatOpenAI, False),
    ("glm-cn", "https://open.bigmodel.cn/api/paas/v4/", NormalizedChatOpenAI, False),
    ("minimax", "https://api.minimax.io/v1", MinimaxChatOpenAI, False),
    ("minimax-cn", "https://api.minimaxi.com/v1", MinimaxChatOpenAI, False),
    ("openrouter", "https://openrouter.ai/api/v1", NormalizedChatOpenAI, False),
    ("mistral", "https://api.mistral.ai/v1", NormalizedChatOpenAI, False),
    ("kimi", "https://api.moonshot.ai/v1", NormalizedChatOpenAI, False),
    ("groq", "https://api.groq.com/openai/v1", NormalizedChatOpenAI, False),
    ("nvidia", "https://integrate.api.nvidia.com/v1", NormalizedChatOpenAI, False),
    ("ollama", "http://localhost:11434/v1", NormalizedChatOpenAI, False),
])
def test_registry_spec(provider, base_url, chat_class, responses):
    """각 제공자의 기본 URL, 채팅 클래스, Responses API 설정이 기대값과 일치하는지 검증하는 테스트."""
    spec = OPENAI_COMPATIBLE_PROVIDERS[provider]
    assert spec.base_url == base_url
    assert spec.chat_class is chat_class
    assert spec.use_responses_api is responses


@pytest.mark.unit
def test_key_optionality():
    """제공자별 API 키 필수/선택 설정이 올바른지 검증하는 테스트."""
    # 로컬/범용 엔드포인트는 키 선택(key-optional); 호스팅 API는 키가 필수다.
    assert OPENAI_COMPATIBLE_PROVIDERS["ollama"].key_optional is True
    assert OPENAI_COMPATIBLE_PROVIDERS["openai_compatible"].key_optional is True
    assert OPENAI_COMPATIBLE_PROVIDERS["openai_compatible"].require_base_url is True
    assert OPENAI_COMPATIBLE_PROVIDERS["xai"].key_optional is False
    # 기본 URL을 재정의하는 환경 변수는 OLLAMA_BASE_URL이 유일하다.
    assert OPENAI_COMPATIBLE_PROVIDERS["ollama"].base_url_env == "OLLAMA_BASE_URL"
