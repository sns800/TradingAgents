# =============================================================================
# [모듈 개요 — 초보자용]
# 이 파일은 LLM에게 정해진 형식(Pydantic 스키마)으로 답하게 하는 구조화 출력
# (structured output) 호출과, 실패 시 자유 텍스트(free-text)로 우아하게
# 대체(fallback)하는 공용 헬퍼를 제공합니다. TradingAgents에서 포트폴리오 매니저,
# 트레이더, 리서치 매니저가 모두 이 패턴을 공유하여, 어떤 LLM 공급자(provider)를
# 쓰더라도 파이프라인이 중간에 멈추지 않도록 보장합니다.
# =============================================================================

"""구조화 출력으로 에이전트를 호출하고 실패 시 우아하게 대체하는 공용 헬퍼.

포트폴리오 매니저(Portfolio Manager), 트레이더(Trader), 리서치 매니저
(Research Manager)는 모두 다음의 표준 패턴을 따른다:

1. 에이전트 생성 시 LLM을 ``with_structured_output(Schema)``로 감싸서
   모델이 타입이 지정된 Pydantic 인스턴스를 반환하게 한다. 공급자가
   구조화 출력을 지원하지 않으면(드묾; 주로 구형 Ollama 모델) 감싸기를
   건너뛰고 에이전트는 자유 텍스트 생성을 사용한다.
2. 호출 시 구조화 호출을 실행하고 결과를 다시 마크다운으로 렌더링한다.
   구조화 호출 자체가 어떤 이유로든 실패하면(약한 모델의 잘못된 JSON,
   일시적 공급자 문제) 일반 ``llm.invoke``로 대체하여 파이프라인이
   절대 막히지 않게 한다.

패턴을 여기 한곳에 집중시켜 에이전트 팩토리를 작게 유지하고, 세 에이전트
모두 대체(fallback)가 발동할 때 동일한 경고를 로깅하도록 보장한다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# 스키마 전용 구조화 출력은 정확히 하나의 툴(스키마 자체)만 바인딩하므로,
# 모델이 검색 툴을 쓰려고 하면 알 수 없는 툴 호출이 발생해 구조화 시도 전체가
# 버려지고 자유 텍스트로 재시도된다. 이 경로의 에이전트들은 바인딩에만 의존하지
# 않고 제약을 명시적으로 프롬프트에 서술한다(#1130).
# (아래 문자열은 LLM 프롬프트에 그대로 들어가므로 영어 원문을 유지한다.)
NO_EXTERNAL_TOOLS = (
    "Use only the evidence provided in this prompt. Do not call external tools "
    "or search the web; if something is missing, say so explicitly."
)


def bind_structured(llm: Any, schema: type[T], agent_name: str) -> Any | None:
    """``llm.with_structured_output(schema)``를 반환하고, 미지원이면 ``None``을 반환한다.

    바인딩이 실패하면 경고를 로깅하여, 에이전트가 1회성 대체가 아니라
    매 호출마다 자유 텍스트 생성을 쓰게 된다는 것을 사용자가 알 수 있게 한다.
    """
    try:
        return llm.with_structured_output(schema)
    except (NotImplementedError, AttributeError) as exc:
        logger.warning(
            "%s: provider does not support with_structured_output (%s); "
            "falling back to free-text generation",
            agent_name, exc,
        )
        return None


def invoke_structured_or_freetext(
    structured_llm: Any | None,
    plain_llm: Any,
    prompt: Any,
    render: Callable[[T], str],
    agent_name: str,
) -> str:
    """구조화 호출을 실행해 마크다운으로 렌더링하고, 실패 시 자유 텍스트로 대체한다.

    ``prompt``는 하부 LLM이 받아들이는 형식 그대로다(채팅 호출이면 문자열,
    그런 형태를 받는 채팅 모델이면 메시지 딕셔너리 리스트). 같은 값이
    자유 텍스트 경로에도 그대로 전달되므로, 대체(fallback) 호출도 구조화
    호출과 동일한 입력을 본다.
    """
    if structured_llm is not None:
        try:
            result = structured_llm.invoke(prompt)
            if result is None:
                # 사고형(thinking) 모델은 툴을 호출하는 대신 일반 텍스트로
                # 답할 수 있고, 그러면 파서가 반환할 것이 없다. 이를 구조화
                # 실패로 간주하고 명확한 사유와 함께 대체 경로로 넘어간다.
                raise ValueError("structured output returned no parsed result")
            return render(result)
        except Exception as exc:
            logger.warning(
                "%s: structured-output invocation failed (%s); retrying once as free text",
                agent_name, exc,
            )

    response = plain_llm.invoke(prompt)
    return response.content
