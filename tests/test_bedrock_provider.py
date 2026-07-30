# 이 파일은 Amazon Bedrock LLM 제공자 지원을 검증하는 테스트 모음입니다.
# 팩토리 라우팅, AWS 자격 증명 체인 기반 인증, 선택적 의존성(langchain-aws)
# 미설치 시의 안내 오류 등을 확인합니다.
"""Amazon Bedrock — 선택적 langchain-aws 엑스트라(extra)를 통한 정식 네이티브 클라이언트 테스트.

인증은 AWS 자격 증명 체인(credential chain)을 사용하므로 단일 키 환경 변수가
없습니다. 모델은 Bedrock 모델 ID / 추론 프로필(inference profile) ID이며,
langchain-aws는 지연 임포트(lazy import)되어 [bedrock] 엑스트라가 없을 때
명확한 설치 안내 메시지를 보여 줍니다.
"""
import sys

import pytest

from tradingagents.llm_clients.api_key_env import get_api_key_env
from tradingagents.llm_clients.factory import create_llm_client
from tradingagents.llm_clients.validators import validate_model


@pytest.mark.unit
def test_factory_routes_bedrock():
    """팩토리(factory)가 "bedrock" 제공자를 BedrockClient로 라우팅하는지 검증하는 테스트."""
    client = create_llm_client("bedrock", "us.anthropic.claude-opus-4-8-v1:0")
    assert type(client).__name__ == "BedrockClient"


@pytest.mark.unit
def test_bedrock_any_model_and_no_key_env():
    """Bedrock은 모든 모델 ID를 허용하고 단일 API 키 환경 변수가 없음을 검증하는 테스트."""
    assert validate_model("bedrock", "any.model-id:0") is True
    # Bedrock은 AWS 자격 증명 체인을 사용하므로 단일 키 환경 변수가 없습니다.
    assert get_api_key_env("bedrock") is None


@pytest.mark.unit
def test_helpful_error_when_langchain_aws_absent(monkeypatch):
    """langchain-aws 미설치 시 설치 안내가 담긴 ImportError가 발생하는지 검증하는 테스트."""
    import tradingagents.llm_clients.bedrock_client as bc
    monkeypatch.setattr(bc, "_BEDROCK_CLASS", None)
    monkeypatch.setitem(sys.modules, "langchain_aws", None)  # 임포트 시 ImportError를 강제로 유발
    with pytest.raises(ImportError, match=r"bedrock"):
        create_llm_client("bedrock", "m").get_llm()


def _capture_kwargs(monkeypatch):
    """_bedrock_class를 가짜(stub)로 바꿔, 선택적 langchain-aws 엑스트라가
    설치되어 있지 않아도 생성자 키워드 인자를 검사할 수 있게 하는 헬퍼."""
    import tradingagents.llm_clients.bedrock_client as bc
    captured = {}

    class _FakeChat:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(bc, "_bedrock_class", lambda: _FakeChat)
    return captured


@pytest.mark.unit
def test_bearer_token_passed_as_api_key(monkeypatch):
    """베어러 토큰(bearer token) 환경 변수가 api_key로 전달되는지 검증하는 테스트 (#1103)."""
    # #1103: Bedrock API 키는 AWS 액세스 키 없이도 인증이 가능합니다.
    captured = _capture_kwargs(monkeypatch)
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bt-secret")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    create_llm_client("bedrock", "us.anthropic.claude-opus-4-8-v1:0").get_llm()
    assert captured["api_key"] == "bt-secret"
    assert captured["region_name"] == "us-east-1"


@pytest.mark.unit
def test_no_bearer_token_omits_api_key(monkeypatch):
    """베어러 토큰이 없으면 api_key 인자를 생략하는지 검증하는 테스트."""
    # 토큰이 없으면 AWS 자격 증명 체인으로 대체합니다 (api_key 키워드 인자 없음).
    captured = _capture_kwargs(monkeypatch)
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    create_llm_client("bedrock", "us.anthropic.claude-opus-4-8-v1:0").get_llm()
    assert "api_key" not in captured


@pytest.mark.unit
def test_construction_when_extra_installed(monkeypatch):
    """langchain-aws가 실제로 설치된 환경에서 클라이언트가 정상 생성되는지 검증하는 테스트."""
    pytest.importorskip("langchain_aws")
    import tradingagents.llm_clients.bedrock_client as bc
    monkeypatch.setattr(bc, "_BEDROCK_CLASS", None)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
    llm = create_llm_client("bedrock", "us.anthropic.claude-sonnet-5").get_llm()
    assert type(llm).__name__ == "NormalizedChatBedrockConverse"
    assert llm.region_name == "eu-west-1"
