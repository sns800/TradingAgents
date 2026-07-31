# =============================================================================
# [모듈 개요 - 초보자용]
# 이 파일은 Amazon Bedrock용 LLM 클라이언트입니다. Bedrock은 AWS에서 여러
# 모델(Claude 등)을 호스팅하는 서비스로, Converse API를 통해 호출합니다.
# 선택적 의존성(optional dependency)인 langchain-aws를 필요할 때만 지연
# import(lazy import)해서, Bedrock을 쓰지 않는 사용자는 boto3 설치 없이도
# 패키지를 사용할 수 있게 합니다.
# factory.py가 provider가 "bedrock"일 때 이 클라이언트를 생성합니다.
# =============================================================================
import os
from typing import Any

from .base_client import BaseLLMClient, normalize_content
from .validators import validate_model

# Bedrock에는 전역 기본 리전(region)이 없다; us-west-2가 가장 폭넓은 모델을 호스팅한다.
_DEFAULT_REGION = "us-west-2"
_BEDROCK_CLASS = None


def _bedrock_class():
    """langchain-aws(선택적 ``[bedrock]`` extra)를 지연 import하고, content 출력을
    정규화한 ChatBedrockConverse 하위 클래스를 반환한다.

    선택적 의존성(그리고 boto3)이 패키지의 나머지 부분에서는 필요 없도록
    필요할 때만 import하며, 첫 호출 이후에는 결과를 캐시한다.

    [초보자용 설명] 함수 안에서 import하는 이유: 파일 최상단에서 import하면
    langchain-aws가 설치되지 않은 환경에서는 이 모듈을 불러오는 것만으로
    오류가 난다. 함수 안으로 미루면 실제로 Bedrock을 쓸 때만 필요해진다.
    """
    global _BEDROCK_CLASS
    if _BEDROCK_CLASS is not None:
        return _BEDROCK_CLASS

    try:
        from langchain_aws import ChatBedrockConverse
    except ImportError as exc:
        raise ImportError(
            "AWS Bedrock support requires the optional 'langchain-aws' dependency. "
            'Install it with: pip install "tradingagents[bedrock]"'
        ) from exc

    class NormalizedChatBedrockConverse(ChatBedrockConverse):
        """content 출력을 문자열로 정규화한 ChatBedrockConverse."""

        def invoke(self, input, config=None, **kwargs):
            return normalize_content(super().invoke(input, config, **kwargs))

    _BEDROCK_CLASS = NormalizedChatBedrockConverse
    return _BEDROCK_CLASS


class BedrockClient(BaseLLMClient):
    """Converse API(langchain-aws)를 통한 Amazon Bedrock용 클라이언트.

    인증 방식은 둘 중 하나다: ``AWS_BEARER_TOKEN_BEDROCK``을 통한 Bedrock API 키
    (베어러 토큰(bearer token)) — AWS 액세스 키가 필요 없다 — 또는 표준 AWS
    자격 증명 체인(credential chain)(환경변수, ``~/.aws/credentials``, IAM 역할)
    에 선택적 ``AWS_PROFILE``을 더한 방식. 어느 쪽이든 ``AWS_REGION`` /
    ``AWS_DEFAULT_REGION``은 설정해야 한다(토큰에는 리전 정보가 없다).
    모델 이름은 Bedrock 모델 ID 또는 교차 리전 추론 프로필(cross-region
    inference profile) ID다. 예: ``us.anthropic.claude-opus-4-8-v1:0``
    """

    def get_llm(self) -> Any:
        """설정이 완료된 ChatBedrockConverse 인스턴스를 반환한다."""
        self.warn_if_unknown_model()
        chat_cls = _bedrock_class()

        region = (
            os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or _DEFAULT_REGION
        )
        llm_kwargs = {"model": self.model, "region_name": region}
        # Bedrock API 키는 AWS 액세스 키 없이 인증한다. 이를 api_key로 전달하면
        # langchain-aws가 베어러 인증(bearer auth)을 우선하므로, 주변 환경의
        # AWS_PROFILE / SigV4 자격 증명이 이를 덮어쓸 수 없다 (#1103).
        bearer_token = os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
        if bearer_token:
            llm_kwargs["api_key"] = bearer_token
        for key in ("temperature", "max_tokens", "max_retries", "callbacks"):
            if key in self.kwargs:
                llm_kwargs[key] = self.kwargs[key]
        # botocore의 기본 읽기 타임아웃은 60초라, 긴 보고서 생성(특히 비영어
        # 출력)이 중간에 ReadTimeoutError로 끊길 수 있다. LLM 응답 대기에
        # 충분한 값으로 올린다 (TRADINGAGENTS_BEDROCK_READ_TIMEOUT으로 조정).
        from botocore.config import Config as _BotoConfig
        read_timeout = int(os.environ.get("TRADINGAGENTS_BEDROCK_READ_TIMEOUT", "300"))
        llm_kwargs["config"] = _BotoConfig(
            read_timeout=read_timeout, connect_timeout=10
        )
        return chat_cls(**llm_kwargs)

    def validate_model(self) -> bool:
        """Bedrock용 모델 검증 (모든 모델 ID 허용)."""
        return validate_model("bedrock", self.model)
