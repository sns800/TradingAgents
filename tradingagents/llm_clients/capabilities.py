# =============================================================================
# [모듈 개요 - 초보자용]
# 이 파일은 "모델별 기능(capability) 표"입니다. OpenAI 호환(OpenAI-compatible)
# 프로바이더의 모델들은 API 수준에서 받아들이는 파라미터가 제각각인데
# (예: 어떤 모델은 tool_choice를 거부), 그런 차이를 코드 곳곳의 if문 대신
# 이 표 하나에 선언적으로(declaratively) 모아 둡니다.
# openai_client.py의 클라이언트들이 get_capabilities(모델명)를 호출해
# "이 모델에는 어떤 파라미터를 보내야/보내지 말아야 하는가"를 판단합니다.
# =============================================================================
"""OpenAI 호환 프로바이더를 위한 모델별 선언적 기능(capability) 테이블.

어떤 모델 ID가 어떤 API 파라미터를 거부하는지, 또는 어떤 구조화 출력
(structured-output) 방식을 요구하는지를 아는 유일한 장소다. LLM 클라이언트
하위 클래스들은 모델 이름별 ``if`` 사다리를 하드코딩하는 대신
``get_capabilities(model_name)``을 참조하므로, 새 모델(또는 새 프로바이더
특이사항)을 추가할 때는 클라이언트 코드가 아니라 이 테이블만 수정하면 된다.

DeepSeek이 자체 통합 가이드에서 공개하는 모델별 ``compat:`` 플래그 패턴을
차용했다 (예: Oh My Pi 설정 스키마는 ``supportsToolChoice``,
``requiresReasoningContentForToolCalls``를 모델별 선언 필드로 문서화한다).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

StructuredMethod = Literal[
    "function_calling",  # tools를 사용; supports_tool_choice를 따른다
    "json_mode",         # response_format={"type":"json_object"} 사용
    "json_schema",       # response_format={"type":"json_schema",...} 사용
    "none",              # 구조화 출력 불가; 호출자는 자유 텍스트로 폴백(fallback)
]


@dataclass(frozen=True)
class ModelCapabilities:
    """OpenAI 호환 모델이 API 수준에서 무엇을 받아들이는지 나타낸다.

    [초보자용 설명] @dataclass(frozen=True)는 생성 후 값을 바꿀 수 없는
    불변(immutable) 데이터 묶음을 만든다. 각 필드가 곧 "이 모델이 지원하는
    기능" 하나에 대응한다.
    """

    supports_tool_choice: bool
    supports_json_mode: bool
    supports_json_schema: bool
    preferred_structured_method: StructuredMethod
    # DeepSeek의 사고 모드(thinking-mode) 모델은 이전 어시스턴트 턴의
    # reasoning_content를 다음 요청에 되돌려 보내지 않으면 400 오류를 낸다.
    requires_reasoning_content_roundtrip: bool = False
    # MiniMax M2.x 추론(reasoning) 모델은 ``reasoning_split=True``가 있어야
    # <think> 블록이 ``content``를 오염시키지 않고 ``reasoning_details``에
    # 담긴다. 이 플래그는 추론 기능이 없는 MiniMax 모델(Coding Plan,
    # MiniMax-Text-01 등)에서는 거부되므로, 실제로 이를 소비하는 모델에만
    # 설정한다. (#826)
    requires_reasoning_split: bool = False


# DeepSeek의 사고(thinking) 모델은 ``tools`` 배열은 받지만 ``tool_choice``
# 파라미터는 거부한다 (공식 Oh My Pi 통합 가이드 및 이슈 #678의 400 응답).
# 공식 도구 호출(tool-calling) 예제(api-docs.deepseek.com/guides/tool_calls)도
# ``tool_choice`` 없이 ``tools=[...]``만 전달한다 — 우리도 그 패턴을 따라
# supports_tool_choice를 False로 두고 클라이언트가 해당 kwarg를 생략하게 한다.
_DEEPSEEK_THINKING = ModelCapabilities(
    supports_tool_choice=False,
    supports_json_mode=True,
    supports_json_schema=False,
    preferred_structured_method="function_calling",
    requires_reasoning_content_roundtrip=True,
)

_DEEPSEEK_CHAT = ModelCapabilities(
    supports_tool_choice=True,
    supports_json_mode=True,
    supports_json_schema=False,
    preferred_structured_method="function_calling",
)

# MiniMax M2.x 추론 모델은 tools 배열은 받지만, tool_choice 파라미터는
# {"none", "auto"} 열거값(enum)으로만 제한된다
# (platform.minimax.io/docs/api-reference/text-post). Langchain의
# function_calling 경로는 tool_choice를 함수 스펙 dict로 보내는데,
# MiniMax는 이를 400으로 거부한다 — DeepSeek 버그와 같은 형태다.
# supports_tool_choice=False로 두면 NormalizedChatOpenAI의 분기 처리
# (dispatch)가 해당 kwarg를 생략하고, 스키마는 여전히 도구(tool)로 전달된다.
# json_mode의 response_format은 MiniMax-Text-01 전용이며 M2.x에는 해당 없다.
_MINIMAX_THINKING = ModelCapabilities(
    supports_tool_choice=False,
    supports_json_mode=False,
    supports_json_schema=False,
    preferred_structured_method="function_calling",
    requires_reasoning_split=True,
)

_DEFAULT = ModelCapabilities(
    supports_tool_choice=True,
    supports_json_mode=True,
    supports_json_schema=True,
    preferred_structured_method="function_calling",
)


# 정확한 ID 일치가 패턴 일치보다 우선한다.
_BY_ID: dict[str, ModelCapabilities] = {
    "deepseek-chat": _DEEPSEEK_CHAT,
    "deepseek-reasoner": _DEEPSEEK_THINKING,
    "deepseek-v4-flash": _DEEPSEEK_THINKING,
    "deepseek-v4-pro": _DEEPSEEK_THINKING,
    # MiniMax — 공식 전체 모델 라인업 출처:
    # platform.minimax.io/docs/api-reference/text-openai-api
    "MiniMax-M2.7": _MINIMAX_THINKING,
    "MiniMax-M2.7-highspeed": _MINIMAX_THINKING,
    "MiniMax-M2.5": _MINIMAX_THINKING,
    "MiniMax-M2.5-highspeed": _MINIMAX_THINKING,
    "MiniMax-M2.1": _MINIMAX_THINKING,
    "MiniMax-M2.1-highspeed": _MINIMAX_THINKING,
    "MiniMax-M2": _MINIMAX_THINKING,
}

# 향후 호환(forward-compat) 패턴. 새로운 ``deepseek-v5-*`` /
# ``deepseek-reasoner-*`` 또는 ``MiniMax-M3*`` 변형 모델도 사고 모드
# 특이사항을 자동으로 물려받는다.
_BY_PATTERN: list[tuple[re.Pattern[str], ModelCapabilities]] = [
    (re.compile(r"^deepseek-v\d"), _DEEPSEEK_THINKING),
    (re.compile(r"^deepseek-reasoner"), _DEEPSEEK_THINKING),
    (re.compile(r"^MiniMax-M\d"), _MINIMAX_THINKING),
]


def get_capabilities(model_name: str) -> ModelCapabilities:
    """정확한 ID → 패턴 → 기본값 순서로 기능(capabilities)을 결정한다."""
    if model_name in _BY_ID:
        return _BY_ID[model_name]
    for pattern, caps in _BY_PATTERN:
        if pattern.match(model_name):
            return caps
    return _DEFAULT
