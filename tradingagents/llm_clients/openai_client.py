# =============================================================================
# [모듈 개요 - 초보자용]
# 이 파일은 OpenAI 및 "OpenAI 호환(OpenAI-compatible)" API를 쓰는 모든
# 프로바이더(xAI, DeepSeek, Qwen, GLM, MiniMax, OpenRouter, Ollama, 로컬 서버 등)를
# 담당하는 클라이언트입니다. 프로바이더별 차이(엔드포인트 URL, 키 필요 여부,
# 응답 특이사항)는 ProviderSpec 레지스트리 한 곳에 선언적으로 정의하고,
# 모델별 파라미터 특이사항은 capabilities.py의 기능 표를 참조합니다.
# factory.py가 위 프로바이더들에 대해 이 파일의 OpenAIClient를 생성합니다.
# =============================================================================
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI

from .api_key_env import get_api_key_env
from .base_client import BaseLLMClient, normalize_content
from .capabilities import get_capabilities
from .validators import validate_model


class NormalizedChatOpenAI(ChatOpenAI):
    """content 출력 정규화와 기능 표 기반 바인딩을 갖춘 ChatOpenAI.

    Responses API는 content를 타입 블록(reasoning, text 등)의 리스트로
    반환한다. ``invoke``는 하위 단계에서 일관되게 처리할 수 있도록
    문자열로 정규화한다.

    ``with_structured_output``은 모델별 기능 표
    (``capabilities.get_capabilities``)를 참조해 사용할 방식(method)을
    고르고 ``tool_choice``를 보내도 되는지 결정한다. ``tool_choice``를
    거부하는 모델(예: DeepSeek V4와 reasoner — 공식 도구 호출 가이드 기준)도
    스키마는 도구(tool)로 바인딩하되, ``tool_choice`` 파라미터는 보내지
    않는다.

    구조화 출력(structured-output) 이외의 프로바이더별 특이사항
    (예: DeepSeek의 reasoning_content 왕복 처리)은 하위 클래스에 두어
    이 베이스 클래스를 작게 유지한다.
    """

    def invoke(self, input, config=None, **kwargs):
        return normalize_content(super().invoke(input, config, **kwargs))

    def with_structured_output(self, schema, *, method=None, **kwargs):
        caps = get_capabilities(self.model_name)
        if caps.preferred_structured_method == "none":
            raise NotImplementedError(
                f"{self.model_name} has no structured-output method available; "
                f"agent factories will fall back to free-text generation."
            )
        method = method or caps.preferred_structured_method
        # 모델이 tool_choice를 거부하면 langchain이 하드코딩하는 값을
        # 억제한다. 스키마는 여전히 도구로 바인딩된다 — DeepSeek 공식
        # 도구 호출 예제와 정확히 같은 방식이다.
        if method == "function_calling" and not caps.supports_tool_choice:
            kwargs.setdefault("tool_choice", None)
        return super().with_structured_output(schema, method=method, **kwargs)


class LocalCompatibleChatOpenAI(NormalizedChatOpenAI):
    """임의의 로컬 서버(LM Studio, vLLM, llama.cpp — 범용 ``openai_compatible``
    프로바이더 경유)를 위한 OpenAI 호환 클라이언트.

    이들의 도구 호출(tool-calling) 지원은 제각각이며, 상당수는 langchain이
    function-calling 구조화 출력에 보내는 객체 형태의 ``tool_choice``를
    거부한다. 스키마는 도구로 바인딩하되 tool_choice는 강제하지 않아,
    모델 ID의 기능 표와 무관하게 어느 로컬 서버에서든 구조화 출력이
    동작하게 한다 (#1057).
    """

    def with_structured_output(self, schema, *, method=None, **kwargs):
        resolved = method or get_capabilities(self.model_name).preferred_structured_method
        if resolved == "function_calling":
            kwargs.setdefault("tool_choice", None)
        return super().with_structured_output(schema, method=method, **kwargs)


def _input_to_messages(input_: Any) -> list:
    """langchain LLM 입력을 메시지 객체의 리스트로 정규화한다.

    메시지 리스트, (ChatPromptTemplate에서 온) ``ChatPromptValue``,
    그 외의 것(메시지 없음으로 취급)을 모두 받는다.
    나가는 메시지 이력을 순회해야 하는 프로바이더가 사용한다;
    특히 DeepSeek 사고 모드(thinking-mode) 전파는 순수 리스트 호출과
    ChatPromptTemplate 기반 호출 모두에서 동작해야 하므로, 여기서
    ``list``만 처리하면 호출 지점의 절반을 조용히 건너뛰게 된다.
    """
    if isinstance(input_, list):
        return input_
    if hasattr(input_, "to_messages"):
        return input_.to_messages()
    return []


class DeepSeekChatOpenAI(NormalizedChatOpenAI):
    """OpenAI 호환 클라이언트 위에 얹은 DeepSeek 전용 오버라이드(override).

    사고 모드(thinking-mode) 왕복 처리만이 여기에 남는 DeepSeek 전용
    동작이다. DeepSeek의 사고 모델이 ``reasoning_content``가 담긴 응답을
    반환하면, 다음 턴에서 그 필드를 어시스턴트 메시지의 일부로 되돌려
    보내야 하며 그러지 않으면 API가 HTTP 400으로 실패한다.
    ``_create_chat_result``가 수신 시 이를 붙잡아 두고,
    ``_get_request_payload``가 송신 시 다시 붙인다.

    V4와 reasoner의 tool_choice 처리 — 이 모델들은 ``tool_choice``
    파라미터를 거부한다 — 는 여기가 아니라
    ``NormalizedChatOpenAI.with_structured_output``의 기능 표 분기가 담당한다.
    """

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        outgoing = payload.get("messages", [])
        # [초보자용 설명] 전송 직전의 dict 형태 메시지와 원본 메시지 객체를
        # 짝지어 순회하면서, AI 메시지에 저장해 둔 reasoning_content가 있으면
        # 요청 페이로드(payload)에 다시 붙인다.
        for message_dict, message in zip(outgoing, _input_to_messages(input_), strict=False):
            if not isinstance(message, AIMessage):
                continue
            reasoning = message.additional_kwargs.get("reasoning_content")
            if reasoning is not None:
                message_dict["reasoning_content"] = reasoning
        return payload

    def _create_chat_result(self, response, generation_info=None):
        chat_result = super()._create_chat_result(response, generation_info)
        response_dict = (
            response
            if isinstance(response, dict)
            else response.model_dump(
                exclude={"choices": {"__all__": {"message": {"parsed"}}}}
            )
        )
        # [초보자용 설명] 응답의 각 선택지(choice)에 reasoning_content가 있으면
        # 메시지의 additional_kwargs에 보관해 두었다가, 다음 요청에서
        # _get_request_payload가 다시 꺼내 쓴다.
        for generation, choice in zip(
            chat_result.generations, response_dict.get("choices", []), strict=False
        ):
            reasoning = choice.get("message", {}).get("reasoning_content")
            if reasoning is not None:
                generation.message.additional_kwargs["reasoning_content"] = reasoning
        return chat_result


class MinimaxChatOpenAI(NormalizedChatOpenAI):
    """OpenAI 호환 클라이언트 위에 얹은 MiniMax 전용 오버라이드(override).

    M2.x 추론(reasoning) 모델은 기본적으로 ``<think>...</think>`` 블록을
    ``message.content`` 안에 직접 넣어 반환해, 저장되는 리포트를 오염시킬
    수 있다. platform.minimax.io/docs/api-reference/text-openai-api에 따라
    ``reasoning_split=True``를 주면 사고 블록이 ``reasoning_details``로
    분리되어 ``content``가 깨끗하게 유지된다. 이 값은 최상위 kwarg가 아니라
    ``extra_body``로 보낸다 — openai SDK가 최상위 파라미터를 검증해서
    reasoning_split 같은 알 수 없는 파라미터를 거부하기 때문이다 (#826).

    이 플래그는 ``ModelCapabilities.requires_reasoning_split``으로 게이트되어
    M2.x 추론 모델만 받는다; 비추론 MiniMax 엔드포인트(Coding Plan,
    MiniMax-Text-01)에는 절대 전달되지 않는다.

    M2.x의 tool_choice 처리 — 이 모델들은 문자열 열거값 ``{"none", "auto"}``
    만 허용하고 langchain의 함수 스펙 dict를 거부한다 — 는 여기가 아니라
    ``NormalizedChatOpenAI.with_structured_output``의 기능 표 분기가 담당한다.
    """

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        if get_capabilities(self.model_name).requires_reasoning_split:
            # 최상위 kwarg가 아니라 extra_body로 전달한다: openai SDK(>=1.56)는
            # 최상위 파라미터를 Completions.create 기준으로 검증해
            # reasoning_split 같은 알 수 없는 파라미터를 거부한다 (#826).
            # extra_body는 요청 본문(body)에 그대로 전달된다.
            extra_body = payload.setdefault("extra_body", {})
            extra_body.setdefault("reasoning_split", True)
        return payload


# 사용자 설정에서 ChatOpenAI로 전달(forward)되는 kwargs
_PASSTHROUGH_KWARGS = (
    "timeout", "max_retries", "reasoning_effort", "temperature",
    "api_key", "callbacks", "http_client", "http_async_client",
)

# OpenAI의 ``reasoning_effort``는 추론(reasoning) 모델 — GPT-5 계열과
# o-시리즈 — 에서만 허용된다. 비추론 모델(gpt-4.1, gpt-4o, ...)은
# "Unsupported parameter: 'reasoning.effort' is not supported with this model"
# 과 함께 400 오류를 낸다. 그런 모델에서는 실행을 중단시키는 대신
# 이 kwarg를 버린다.
_OPENAI_REASONING_MODEL = re.compile(r"^(gpt-5|o[1-9])")


def _supports_reasoning_effort(model: str) -> bool:
    """(네이티브 OpenAI) 모델이 ``reasoning_effort``를 허용하는지 여부."""
    return bool(_OPENAI_REASONING_MODEL.match(model.lower().strip()))


@dataclass(frozen=True)
class ProviderSpec:
    """OpenAI 호환 프로바이더 하나에 대한 선언적(declarative) 설정.

    OpenAI 호환 계열(OpenAI, xAI, DeepSeek, Qwen, GLM, MiniMax, OpenRouter,
    Ollama, 그리고 임의의 사용자 엔드포인트)은 모두 같은 Chat Completions
    API를 사용하며 아래 필드들만 다르다 — 그래서 여기 한 행(row)이 과거의
    프로바이더별 base URL dict, 인증 처리, 클라이언트 클래스 분기문을
    대체한다. 네이티브 Anthropic / Google은 (실제로 다른 API이므로) 자체
    클라이언트를 사용하며 의도적으로 이 레지스트리에 넣지 않았다.

    API 키 환경변수는 ``api_key_env.PROVIDER_API_KEY_ENV``(이 클라이언트와
    CLI 프롬프트가 함께 참조하는 단일 공급원)에 그대로 두고, 프로바이더별로
    다른 동작(base URL, 키 선택 여부, ``chat_class``를 통한 와이어 포맷
    특이사항)만 여기에 둔다.
    """

    chat_class: type = NormalizedChatOpenAI   # 프로바이더 특이사항은 하위 클래스에 둔다
    base_url: str | None = None            # 기본 엔드포인트 (None -> SDK 기본값)
    base_url_env: str | None = None        # base_url을 덮어쓰는 환경변수 (예: OLLAMA_BASE_URL)
    key_optional: bool = False                # 요구/프롬프트하지 않음; 미설정 시 자리 표시 값 전송
    placeholder_key: str = "EMPTY"            # 키가 없을 때 보내는 값 (키 없는 로컬 서버용)
    require_base_url: bool = False            # base_url이 결정되지 않으면 오류 (범용 엔드포인트)
    use_responses_api: bool = False           # 네이티브 OpenAI Responses API 사용


# OpenAI 호환 프로바이더 계열의 단일 진실 공급원(single source of truth).
# 이중 리전(dual-region) 프로바이더(qwen/glm/minimax)는 국제 계정과 중국
# 계정이 자격 증명을 공유할 수 없어 엔드포인트를 따로 유지한다 (#758).
OPENAI_COMPATIBLE_PROVIDERS: dict[str, ProviderSpec] = {
    "openai":     ProviderSpec(use_responses_api=True),
    "xai":        ProviderSpec(base_url="https://api.x.ai/v1"),
    "deepseek":   ProviderSpec(base_url="https://api.deepseek.com", chat_class=DeepSeekChatOpenAI),
    "qwen":       ProviderSpec(base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1"),
    "qwen-cn":    ProviderSpec(base_url="https://dashscope.aliyuncs.com/compatible-mode/v1"),
    "glm":        ProviderSpec(base_url="https://api.z.ai/api/paas/v4/"),
    "glm-cn":     ProviderSpec(base_url="https://open.bigmodel.cn/api/paas/v4/"),
    "minimax":    ProviderSpec(base_url="https://api.minimax.io/v1", chat_class=MinimaxChatOpenAI),
    "minimax-cn": ProviderSpec(base_url="https://api.minimaxi.com/v1", chat_class=MinimaxChatOpenAI),
    "openrouter": ProviderSpec(base_url="https://openrouter.ai/api/v1"),
    "mistral":    ProviderSpec(base_url="https://api.mistral.ai/v1"),
    "kimi":       ProviderSpec(base_url="https://api.moonshot.ai/v1"),
    "groq":       ProviderSpec(base_url="https://api.groq.com/openai/v1"),
    "nvidia":     ProviderSpec(base_url="https://integrate.api.nvidia.com/v1"),
    "ollama":     ProviderSpec(base_url="http://localhost:11434/v1", base_url_env="OLLAMA_BASE_URL",
                               key_optional=True, placeholder_key="ollama"),
    # 범용 엔드포인트: 사용자가 base_url을 제공; 키는 선택 (키 없는 로컬 서버).
    "openai_compatible": ProviderSpec(
        require_base_url=True, key_optional=True, chat_class=LocalCompatibleChatOpenAI
    ),
}


def is_openai_compatible(provider: str) -> bool:
    """``provider``가 OpenAI 호환 레지스트리에서 서비스되는지 여부."""
    return provider.lower() in OPENAI_COMPATIBLE_PROVIDERS


def _is_native_openai_base_url(base_url: str | None) -> bool:
    """``base_url``이 비어 있거나 api.openai.com을 가리킬 때 True.

    Responses API(/v1/responses)는 네이티브 OpenAI에만 존재한다. ``openai``
    프로바이더에 커스텀 base_url(프록시, 게이트웨이, 로컬 서버)을 지정하면
    그쪽은 Chat Completions만 지원하므로, 프로바이더 스펙이 Responses API를
    켜 두었더라도 그 경우엔 꺼진 상태를 유지해야 한다 (#1024).
    """
    if not base_url:
        return True
    if "://" not in base_url:
        base_url = "https://" + base_url
    host = urlparse(base_url).hostname or ""
    return host == "api.openai.com" or host.endswith(".openai.com")


class OpenAIClient(BaseLLMClient):
    """OpenAI, Ollama, OpenRouter, xAI 등 프로바이더용 클라이언트.

    네이티브 OpenAI 모델에는 Responses API(/v1/responses)를 사용한다.
    이 API는 모든 모델 계열(GPT-4.1, GPT-5)에서 함수 도구(function tools)와
    함께 reasoning_effort를 지원한다. 서드파티 호환 프로바이더(xAI,
    OpenRouter, Ollama)는 표준 Chat Completions를 사용한다.
    """

    def __init__(
        self,
        model: str,
        base_url: str | None = None,
        provider: str = "openai",
        **kwargs,
    ):
        super().__init__(model, base_url, **kwargs)
        self.provider = provider.lower()

    def get_llm(self) -> Any:
        """프로바이더 레지스트리를 기반으로 설정된 ChatOpenAI 인스턴스를 반환한다."""
        self.warn_if_unknown_model()
        llm_kwargs = {"model": self.model}
        spec = OPENAI_COMPATIBLE_PROVIDERS.get(self.provider)
        chat_cls = NormalizedChatOpenAI

        if spec is not None:
            chat_cls = spec.chat_class

            # base_url 우선순위: 명시적 클라이언트 base_url (설정 /
            # TRADINGAGENTS_LLM_BACKEND_URL 값을 담는다) > 프로바이더 환경변수
            # 오버라이드 (예: OLLAMA_BASE_URL) > 프로바이더 기본값.
            # None이면 SDK 기본값을 사용한다.
            env_base_url = os.environ.get(spec.base_url_env) if spec.base_url_env else None
            base_url = self.base_url or env_base_url or spec.base_url
            if spec.require_base_url and not base_url:
                raise ValueError(
                    f"Provider '{self.provider}' requires a base_url. Set it via "
                    "backend_url / TRADINGAGENTS_LLM_BACKEND_URL to your endpoint, "
                    "e.g. http://localhost:8000/v1 (vLLM) or http://localhost:1234/v1 "
                    "(LM Studio)."
                )
            if base_url:
                llm_kwargs["base_url"] = base_url

            # API 키: key_optional이 아닌 한 필수; 키 없는 로컬 서버에는
            # 자리 표시 값(placeholder)을 준다. 환경변수 이름의 단일 공급원은
            # api_key_env다.
            api_key_env = get_api_key_env(self.provider)
            api_key = os.environ.get(api_key_env) if api_key_env else None
            if api_key:
                llm_kwargs["api_key"] = api_key
            elif spec.key_optional:
                llm_kwargs["api_key"] = spec.placeholder_key
            elif api_key_env:
                raise ValueError(
                    f"API key for provider '{self.provider}' is not set. "
                    f"Please set the {api_key_env} environment variable "
                    f"(e.g. add {api_key_env}=your_key to your .env file)."
                )

            # Responses API는 네이티브 OpenAI에만 존재한다; 사용자가 openai
            # 프로바이더를 커스텀 base_url(프록시/게이트웨이/로컬)로 돌리면
            # 그쪽은 Chat Completions만 지원하므로 Responses를 끈 채 둔다 (#1024).
            if spec.use_responses_api and _is_native_openai_base_url(base_url):
                llm_kwargs["use_responses_api"] = True
        elif self.base_url:
            llm_kwargs["base_url"] = self.base_url

        # 사용자가 제공한 kwargs를 전달한다
        for key in _PASSTHROUGH_KWARGS:
            if key not in self.kwargs:
                continue
            # [초보자용 설명] reasoning_effort(추론 강도)는 지원 모델에만
            # 전달한다. 미지원 모델에 보내면 API가 400 오류를 내기 때문이다.
            if key == "reasoning_effort" and not _supports_reasoning_effort(self.model):
                continue
            llm_kwargs[key] = self.kwargs[key]

        # 하위 클래스(프로바이더 특이사항)는 레지스트리 스펙에서 온다.
        return chat_cls(**llm_kwargs)

    def validate_model(self) -> bool:
        """해당 프로바이더에 대해 모델 이름을 검증한다."""
        return validate_model(self.provider, self.model)
