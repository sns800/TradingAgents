# =============================================================================
# [모듈 개요 - 초보자용]
# 이 파일은 Azure OpenAI 배포(deployment)용 LLM 클라이언트입니다.
# Azure는 모델을 직접 고르는 대신 사용자가 만든 "배포 이름(deployment name)"으로
# 접근하므로, 환경변수에서 배포 이름과 엔드포인트 정보를 읽어
# langchain의 AzureChatOpenAI를 구성합니다.
# factory.py가 provider가 "azure"일 때 이 클라이언트를 생성합니다.
# =============================================================================
import os
from typing import Any

from langchain_openai import AzureChatOpenAI

from .base_client import BaseLLMClient, normalize_content

# [초보자용 설명] 사용자 설정에서 AzureChatOpenAI로 그대로 전달해도 안전한
# kwargs의 허용 목록(allowlist).
_PASSTHROUGH_KWARGS = (
    "timeout", "max_retries", "api_key", "reasoning_effort", "temperature",
    "callbacks", "http_client", "http_async_client",
)


class NormalizedAzureChatOpenAI(AzureChatOpenAI):
    """content 출력을 정규화한 AzureChatOpenAI."""

    def invoke(self, input, config=None, **kwargs):
        return normalize_content(super().invoke(input, config, **kwargs))


class AzureOpenAIClient(BaseLLMClient):
    """Azure OpenAI 배포(deployment)용 클라이언트.

    다음 환경변수가 필요하다:
        AZURE_OPENAI_API_KEY: API 키
        AZURE_OPENAI_ENDPOINT: 엔드포인트 URL (예: https://<resource>.openai.azure.com/)
        AZURE_OPENAI_DEPLOYMENT_NAME: 배포 이름
        OPENAI_API_VERSION: API 버전 (예: 2025-03-01-preview)
    """

    def __init__(self, model: str, base_url: str | None = None, **kwargs):
        super().__init__(model, base_url, **kwargs)

    def get_llm(self) -> Any:
        """설정이 완료된 AzureChatOpenAI 인스턴스를 반환한다."""
        self.warn_if_unknown_model()

        # [초보자용 설명] Azure에서는 "배포 이름"이 실제 호출 대상이다.
        # 환경변수가 없으면 모델 이름을 배포 이름으로 그대로 사용한다.
        llm_kwargs = {
            "model": self.model,
            "azure_deployment": os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME", self.model),
        }

        for key in _PASSTHROUGH_KWARGS:
            if key in self.kwargs:
                llm_kwargs[key] = self.kwargs[key]

        return NormalizedAzureChatOpenAI(**llm_kwargs)

    def validate_model(self) -> bool:
        """Azure는 배포된 어떤 모델 이름이든 허용한다."""
        return True
