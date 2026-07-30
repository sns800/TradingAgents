# =============================================================================
# [모듈 개요 - 초보자용]
# 이 파일은 Google Gemini 모델용 LLM 클라이언트입니다.
# langchain의 ChatGoogleGenerativeAI를 감싸서 (1) 응답 content를 문자열로
# 정규화하고, (2) 통일된 api_key 설정을 Google 전용 google_api_key로 매핑하며,
# (3) 사고 수준(thinking_level) 파라미터를 모델별 허용 범위에 맞게 조정합니다.
# factory.py가 provider가 "google"일 때 이 클라이언트를 생성합니다.
# =============================================================================
from typing import Any

from langchain_google_genai import ChatGoogleGenerativeAI

from .base_client import BaseLLMClient, normalize_content
from .validators import validate_model


class NormalizedChatGoogleGenerativeAI(ChatGoogleGenerativeAI):
    """content 출력을 정규화한 ChatGoogleGenerativeAI.

    Gemini 3 모델은 content를 타입 블록의 리스트로 반환한다.
    하위 단계에서 일관되게 처리할 수 있도록 문자열로 정규화한다.
    """

    def invoke(self, input, config=None, **kwargs):
        return normalize_content(super().invoke(input, config, **kwargs))


class GoogleClient(BaseLLMClient):
    """Google Gemini 모델용 클라이언트."""

    def __init__(self, model: str, base_url: str | None = None, **kwargs):
        super().__init__(model, base_url, **kwargs)

    def get_llm(self) -> Any:
        """설정이 완료된 ChatGoogleGenerativeAI 인스턴스를 반환한다."""
        self.warn_if_unknown_model()
        llm_kwargs = {"model": self.model}

        if self.base_url:
            llm_kwargs["base_url"] = self.base_url

        for key in ("timeout", "max_retries", "temperature", "callbacks", "http_client", "http_async_client"):
            if key in self.kwargs:
                llm_kwargs[key] = self.kwargs[key]

        # 통일된 api_key 설정을 프로바이더 전용 google_api_key로 매핑한다
        google_api_key = self.kwargs.get("api_key") or self.kwargs.get("google_api_key")
        if google_api_key:
            llm_kwargs["google_api_key"] = google_api_key

        # Gemini 3.x는 문자열 ``thinking_level``을 받는다 (정수형
        # ``thinking_budget``은 이제 은퇴한 2.5 라인용이었다). Pro는
        # low/high만 허용하고, Flash는 minimal/medium도 허용한다 — 따라서
        # Pro에서 지원되지 않는 "minimal"은 Pro가 허용하는 가장 가까운
        # 수준으로 매핑한다.
        thinking_level = self.kwargs.get("thinking_level")
        if thinking_level:
            if "pro" in self.model.lower() and thinking_level == "minimal":
                thinking_level = "low"
            llm_kwargs["thinking_level"] = thinking_level

        return NormalizedChatGoogleGenerativeAI(**llm_kwargs)

    def validate_model(self) -> bool:
        """Google용 모델 이름을 검증한다."""
        return validate_model("google", self.model)
