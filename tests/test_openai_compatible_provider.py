"""[모듈 개요] 범용 OpenAI 호환(OpenAI-compatible) 제공자를 검증하는 테스트
(vLLM / LM Studio / llama.cpp / 중계 서버(relay) 등).

사용자가 지정한 base_url이 필수이며 그대로 사용되는지, API 키는 선택 사항인지
(키 없는 로컬 서버가 기본), Responses API가 아닌 Chat Completions를 쓰는지,
어떤 모델 이름이든 허용되는지, 환경 변수 백엔드 URL 우선순위(#978)를 검증한다.
"""

import pytest

from tradingagents.llm_clients.api_key_env import get_api_key_env
from tradingagents.llm_clients.factory import create_llm_client
from tradingagents.llm_clients.validators import validate_model

# 참고: isinstance가 아니라 클래스 이름(NAME)으로 검증한다 — 다른 테스트들이
# openai_client 모듈을 리로드하면 두 번째 클래스 정체성이 생겨 isinstance가 깨지기 때문.


@pytest.mark.unit
def test_factory_routes_to_openai_client():
    """팩토리(factory)가 openai_compatible 제공자를 OpenAIClient로 라우팅하는지 검증하는 테스트."""
    client = create_llm_client(
        provider="openai_compatible", model="my-model", base_url="http://localhost:8000/v1"
    )
    assert type(client).__name__ == "OpenAIClient"


@pytest.mark.unit
def test_base_url_required(monkeypatch):
    """base_url을 지정하지 않으면 명확한 오류가 발생하는지 검증하는 테스트."""
    monkeypatch.delenv("OPENAI_COMPATIBLE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="requires a base_url"):
        create_llm_client(provider="openai_compatible", model="m").get_llm()


@pytest.mark.unit
def test_keyless_local_uses_placeholder_and_chat_completions(monkeypatch):
    """키 없는 로컬 서버에는 자리 표시(placeholder) 키를 보내고 Chat Completions를 쓰는지 검증하는 테스트."""
    monkeypatch.delenv("OPENAI_COMPATIBLE_API_KEY", raising=False)
    llm = create_llm_client(
        provider="openai_compatible", model="qwen2.5", base_url="http://localhost:8000/v1"
    ).get_llm()
    assert type(llm).__name__ == "LocalCompatibleChatOpenAI"
    assert str(llm.openai_api_base) == "http://localhost:8000/v1"
    # 키 없는 로컬 서버: 자리 표시(placeholder) 키가 전송된다
    key = llm.openai_api_key.get_secret_value() if hasattr(llm.openai_api_key, "get_secret_value") else llm.openai_api_key
    assert key == "EMPTY"
    # OpenAI의 Responses API가 아니라 Chat Completions를 사용해야 한다
    assert getattr(llm, "use_responses_api", False) in (False, None)


@pytest.mark.unit
def test_optional_key_from_env(monkeypatch):
    """환경 변수에 설정된 선택적 API 키가 클라이언트에 전달되는지 검증하는 테스트."""
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "sk-relay-123")
    llm = create_llm_client(
        provider="openai_compatible", model="m", base_url="https://relay.example/v1"
    ).get_llm()
    key = llm.openai_api_key.get_secret_value() if hasattr(llm.openai_api_key, "get_secret_value") else llm.openai_api_key
    assert key == "sk-relay-123"


@pytest.mark.unit
def test_any_model_accepted_no_forced_key():
    """어떤 모델 이름이든 허용되고 API 키 입력이 강제되지 않는지 검증하는 테스트."""
    assert validate_model("openai_compatible", "literally-anything") is True
    # 키 환경 변수는 존재하지만(키가 필요한 중계 서버용으로 읽음) 제공자가
    # 키 선택(key-optional)으로 표시되어 있어, CLI가 입력을 강제하지 않고
    # 키 없는 서버도 동작한다.
    assert get_api_key_env("openai_compatible") == "OPENAI_COMPATIBLE_API_KEY"
    from tradingagents.llm_clients.openai_client import OPENAI_COMPATIBLE_PROVIDERS
    assert OPENAI_COMPATIBLE_PROVIDERS["openai_compatible"].key_optional is True


@pytest.mark.unit
def test_env_backend_url_precedence():
    """환경 변수로 지정한 백엔드 URL이 메뉴/기본값보다 우선하는지 검증하는 테스트."""
    # #978: 명시적인 환경 변수 URL은 제공자 출처와 무관하게 메뉴/기본값보다 우선한다.
    from cli.utils import resolve_backend_url
    assert resolve_backend_url("openai", "https://api.openai.com/v1", env_url="http://proxy/v1") == "http://proxy/v1"
    assert resolve_backend_url("openai", "https://api.openai.com/v1", env_url=None) == "https://api.openai.com/v1"
    assert resolve_backend_url("deepseek", None, None) == "https://api.deepseek.com"


@pytest.mark.unit
def test_structured_output_suppresses_object_tool_choice(monkeypatch):
    """구조화 출력(structured output) 시 객체 형태의 tool_choice를 억제하는지 검증하는 테스트."""
    # LM Studio / vLLM은 langchain이 함수 호출(function-calling) 구조화 출력을 위해
    # 보내는 객체 형태의 tool_choice를 거부한다 (#1057). 범용 제공자는 스키마를
    # 도구(tool)로 바인딩하되 tool_choice를 강제하면 안 된다.
    from langchain_openai import ChatOpenAI
    from pydantic import BaseModel

    class Schema(BaseModel):
        x: int

    captured = {}
    monkeypatch.setattr(
        ChatOpenAI,
        "with_structured_output",
        lambda self, schema, method=None, **kw: captured.update({"method": method, **kw}) or "BOUND",
    )
    llm = create_llm_client(
        provider="openai_compatible", model="local-llm-30b", base_url="http://localhost:1234/v1"
    ).get_llm()
    out = llm.with_structured_output(Schema)
    assert out == "BOUND"
    assert captured["method"] == "function_calling"
    assert captured["tool_choice"] is None  # 객체 형태가 아니어야 함
