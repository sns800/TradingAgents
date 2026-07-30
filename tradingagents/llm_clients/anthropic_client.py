# =============================================================================
# [모듈 개요 - 초보자용]
# 이 파일은 Anthropic Claude 모델용 LLM 클라이언트입니다.
# langchain의 ChatAnthropic을 감싸서 (1) 응답 content를 문자열로 정규화하고,
# (2) 사용자 설정 kwargs 중 안전한 것만 골라 전달하며, (3) 확장 사고
# (extended thinking)의 ``effort`` 파라미터를 지원하는 모델에만 넘겨줍니다.
# factory.py가 provider가 "anthropic"일 때 이 클라이언트를 생성합니다.
# =============================================================================
import re
from typing import Any

from langchain_anthropic import ChatAnthropic

from .base_client import BaseLLMClient, normalize_content
from .validators import validate_model

# [초보자용 설명] 사용자 설정에서 ChatAnthropic으로 그대로 전달(pass-through)해도
# 안전한 kwargs의 허용 목록(allowlist). 목록에 없는 키는 무시된다.
_PASSTHROUGH_KWARGS = (
    "timeout", "max_retries", "api_key", "max_tokens", "temperature",
    "callbacks", "http_client", "http_async_client", "effort",
)

# Anthropic의 확장 사고(extended-thinking) ``effort`` 파라미터는 Opus 4.5+,
# Sonnet 4.6+, 그리고 Claude 5 계열(Sonnet 5, Fable 5)에서만 허용된다.
# Sonnet 4.5와 모든 Haiku 버전은 ``"This model does not support the effort
# parameter"``와 함께 400 오류를 낸다 (#831). 버전 표기는 점 구분
# (``opus-4-8``)일 수도, 숫자 하나(``sonnet-5``, ``fable-5``)일 수도 있다;
# 아래의 계열별 최소 버전은 향후 버전에도 호환(forward-compatible)된다.
_EFFORT_EXACT = {
    "claude-mythos-preview",  # 비표준 프리뷰(preview) 이름; effort 지원
    "claude-mythos-5",        # Fable 5의 쌍둥이 모델(Project Glasswing); effort 지원
}
_EFFORT_MODEL = re.compile(r"^claude-(opus|sonnet|fable)-(\d+)(?:-(\d+))?$")
_EFFORT_MIN_VERSION = {"opus": (4, 5), "sonnet": (4, 6), "fable": (5, 0)}


def _supports_effort(model: str) -> bool:
    """Anthropic이 이 모델에 대해 ``effort`` 파라미터를 허용하는지 여부."""
    model_lc = model.lower()
    if model_lc in _EFFORT_EXACT:
        return True
    match = _EFFORT_MODEL.match(model_lc)
    if not match:
        return False
    # [초보자용 설명] 모델 이름에서 계열(family)과 버전을 뽑아
    # (메이저, 마이너) 튜플로 만든 뒤, 계열별 최소 지원 버전과 비교한다.
    # 튜플 비교는 사전식(lexicographic)이라 (4, 6) >= (4, 5)처럼 동작한다.
    family = match.group(1)
    major = int(match.group(2))
    minor = int(match.group(3)) if match.group(3) else 0
    return (major, minor) >= _EFFORT_MIN_VERSION[family]


class NormalizedChatAnthropic(ChatAnthropic):
    """content 출력을 정규화한 ChatAnthropic.

    확장 사고(extended thinking)나 도구 사용(tool use)이 있는 Claude 모델은
    content를 타입 블록의 리스트로 반환한다. 하위 단계에서 일관되게 처리할
    수 있도록 문자열로 정규화한다.
    """

    def invoke(self, input, config=None, **kwargs):
        return normalize_content(super().invoke(input, config, **kwargs))


class AnthropicClient(BaseLLMClient):
    """Anthropic Claude 모델용 클라이언트."""

    def __init__(self, model: str, base_url: str | None = None, **kwargs):
        super().__init__(model, base_url, **kwargs)

    def get_llm(self) -> Any:
        """설정이 완료된 ChatAnthropic 인스턴스를 반환한다."""
        self.warn_if_unknown_model()
        llm_kwargs = {"model": self.model}

        if self.base_url:
            llm_kwargs["base_url"] = self.base_url

        # [초보자용 설명] 허용 목록에 있는 kwargs만 전달하되,
        # effort는 해당 모델이 지원할 때만 넘긴다 (미지원 모델은 400 오류).
        for key in _PASSTHROUGH_KWARGS:
            if key not in self.kwargs:
                continue
            if key == "effort" and not _supports_effort(self.model):
                continue
            llm_kwargs[key] = self.kwargs[key]

        return NormalizedChatAnthropic(**llm_kwargs)

    def validate_model(self) -> bool:
        """Anthropic용 모델 이름을 검증한다."""
        return validate_model("anthropic", self.model)
