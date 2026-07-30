# =============================================================================
# [모듈 개요 - 초보자용]
# 이 파일은 모든 LLM 클라이언트가 공통으로 따라야 하는 "설계도" 역할을 합니다.
# - BaseLLMClient: 추상 베이스 클래스(abstract base class). OpenAI, Anthropic 등
#   각 프로바이더(provider)별 클라이언트가 반드시 구현해야 할 메서드를 정의합니다.
# - normalize_content: 프로바이더마다 다른 응답 형식을 하나의 문자열로 통일하는
#   도우미 함수입니다.
# 전체 시스템에서 factory.py가 이 베이스 클래스의 하위 클래스 인스턴스를 만들어
# 트레이딩 에이전트들에게 LLM을 공급합니다.
# =============================================================================
import warnings
from abc import ABC, abstractmethod
from typing import Any


def normalize_content(response):
    """LLM 응답의 content를 일반 문자열(plain string)로 정규화한다.

    여러 프로바이더(OpenAI Responses API, Google Gemini 3)는 content를
    타입이 지정된 블록(typed block)의 리스트로 반환한다.
    예: [{'type': 'reasoning', ...}, {'type': 'text', 'text': '...'}]
    하위 단계의 에이전트들은 response.content가 문자열이기를 기대하므로,
    여기서 텍스트 블록만 추출해 이어 붙이고 추론(reasoning)/메타데이터
    블록은 버린다.
    """
    content = response.content
    if isinstance(content, list):
        # [초보자용 설명] 리스트의 각 항목을 검사해:
        # - dict이면서 type이 "text"인 경우 → 그 텍스트를 사용
        # - 순수 문자열인 경우 → 그대로 사용
        # - 그 외(추론 블록 등) → 빈 문자열로 처리해 결과에서 제외
        texts = [
            item.get("text", "") if isinstance(item, dict) and item.get("type") == "text"
            else item if isinstance(item, str) else ""
            for item in content
        ]
        response.content = "\n".join(t for t in texts if t)
    return response


class BaseLLMClient(ABC):
    """LLM 클라이언트들의 추상 베이스 클래스(abstract base class).

    [초보자용 설명] ABC를 상속하면 이 클래스는 직접 인스턴스를 만들 수 없고,
    @abstractmethod가 붙은 메서드(get_llm, validate_model)를 모두 구현한
    하위 클래스만 인스턴스화할 수 있다. 즉 "모든 클라이언트가 갖춰야 할
    최소한의 인터페이스"를 강제하는 장치다.
    """

    def __init__(self, model: str, base_url: str | None = None, **kwargs):
        self.model = model
        self.base_url = base_url
        self.kwargs = kwargs

    def get_provider_name(self) -> str:
        """경고 메시지에 사용할 프로바이더(provider) 이름을 반환한다."""
        provider = getattr(self, "provider", None)
        if provider:
            return str(provider)
        # [초보자용 설명] provider 속성이 없으면 클래스 이름에서 유추한다.
        # 예: "AnthropicClient" → 뒤의 "Client"를 떼고 소문자로 → "anthropic"
        return self.__class__.__name__.removesuffix("Client").lower()

    def warn_if_unknown_model(self) -> None:
        """모델이 해당 프로바이더의 알려진 목록에 없으면 경고를 출력한다."""
        if self.validate_model():
            return

        warnings.warn(
            (
                f"Model '{self.model}' is not in the known model list for "
                f"provider '{self.get_provider_name()}'. Continuing anyway."
            ),
            RuntimeWarning,
            stacklevel=2,
        )

    @abstractmethod
    def get_llm(self) -> Any:
        """설정이 완료된 LLM 인스턴스를 반환한다."""
        pass

    @abstractmethod
    def validate_model(self) -> bool:
        """이 클라이언트가 해당 모델을 지원하는지 검증한다."""
        pass
