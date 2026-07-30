# 이 파일은 DeepSeek 추론(reasoning) 모델 전용 클라이언트의 동작을 검증하는
# 테스트 모음입니다. 추론 내용(reasoning_content)이 대화 턴 사이에 유지되는지,
# tool_choice를 거부하는 모델에서 구조화 출력이 올바르게 동작하는지 확인합니다.
"""DeepSeekChatOpenAI의 사고 모드(thinking-mode) 동작 테스트.

두 가지를 검증합니다:

1. 응답 수신 시 ``reasoning_content``가 AIMessage의 ``additional_kwargs``에
   저장되고, 송신 시 다시 첨부되어 DeepSeek API가 턴(turn)마다 같은 값을
   볼 수 있어야 합니다.
2. ``with_structured_output``이 기능 표(capability table)를 참조해,
   ``tool_choice``를 거부하는 모델(V4 + reasoner)에서는 이를 생략해야 합니다.
   이는 DeepSeek 공식 도구 호출(tool-calling) 패턴을 따릅니다:
   https://api-docs.deepseek.com/guides/tool_calls
"""

import os

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.prompt_values import ChatPromptValue
from pydantic import BaseModel

from tradingagents.llm_clients.openai_client import (
    DeepSeekChatOpenAI,
    NormalizedChatOpenAI,
    _input_to_messages,
)

# ---------------------------------------------------------------------------
# _input_to_messages — 리스트 / ChatPromptValue / 기타 입력을 처리하는 헬퍼
# (Gemini 봇 리뷰 지적: 리스트가 아닌 입력도 동작해야 함)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestInputToMessages:
    """_input_to_messages 헬퍼의 입력 형태별 처리를 검증하는 테스트 묶음."""

    def test_list_input_returned_as_is(self):
        """리스트 입력은 변환 없이 그대로 반환되는지 검증하는 테스트."""
        msgs = [HumanMessage(content="hi")]
        assert _input_to_messages(msgs) is msgs

    def test_chat_prompt_value_unwrapped(self):
        """ChatPromptValue 입력에서 내부 메시지 리스트를 꺼내는지 검증하는 테스트."""
        msgs = [HumanMessage(content="hi")]
        prompt_value = ChatPromptValue(messages=msgs)
        assert _input_to_messages(prompt_value) == msgs

    def test_string_input_yields_empty_list(self):
        """단순 문자열 입력에는 빈 리스트를 반환하는지 검증하는 테스트."""
        # 문자열 자체는 메시지를 담은 입력이 아닙니다. 호출자의 일반적인
        # langchain 변환은 _get_request_payload보다 앞 단계에서 일어납니다.
        assert _input_to_messages("hello") == []


# ---------------------------------------------------------------------------
# 대화 턴 사이의 추론 내용(reasoning content) 전파
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDeepSeekReasoningContent:
    """추론 내용이 수신 시 저장되고 송신 시 다시 첨부되는지 검증하는 테스트 묶음."""

    def _client(self):
        os.environ.setdefault("DEEPSEEK_API_KEY", "placeholder")
        return DeepSeekChatOpenAI(
            model="deepseek-v4-flash",
            api_key="placeholder",
            base_url="https://api.deepseek.com",
        )

    def test_capture_on_receive(self):
        """응답에 reasoning_content가 있으면 AIMessage의 additional_kwargs에
        저장되어 다음 턴에서 되돌려 보낼 수 있는지 검증하는 테스트."""
        client = self._client()
        result = client._create_chat_result(
            {
                "model": "deepseek-v4-flash",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "Plan: buy NVDA.",
                            "reasoning_content": "Step 1: trend is up. Step 2: ...",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }
        )
        ai = result.generations[0].message
        assert ai.additional_kwargs["reasoning_content"] == "Step 1: trend is up. Step 2: ..."

    def test_propagate_on_send(self):
        """송신하는 AIMessage에 reasoning_content가 있으면 요청 페이로드의
        해당 메시지 dict에도 같은 값이 실리는지 검증하는 테스트."""
        client = self._client()
        prior = AIMessage(
            content="Plan",
            additional_kwargs={"reasoning_content": "weighed bull case"},
        )
        new_user = HumanMessage(content="Refine.")
        payload = client._get_request_payload([prior, new_user])
        # 페이로드에서 assistant 메시지를 찾음
        assistant_dicts = [m for m in payload["messages"] if m.get("role") == "assistant"]
        assert assistant_dicts, "assistant message missing from outgoing payload"
        assert assistant_dicts[0]["reasoning_content"] == "weighed bull case"

    def test_propagate_through_chat_prompt_value(self):
        """리스트가 아닌 입력(ChatPromptValue)에서도 reasoning_content가
        전파되는지 검증하는 테스트 (Gemini 봇 리뷰 지적 사항)."""
        client = self._client()
        prior = AIMessage(
            content="Plan",
            additional_kwargs={"reasoning_content": "weighed bull case"},
        )
        prompt_value = ChatPromptValue(messages=[prior, HumanMessage(content="Refine.")])
        payload = client._get_request_payload(prompt_value)
        assistant_dicts = [m for m in payload["messages"] if m.get("role") == "assistant"]
        assert assistant_dicts[0]["reasoning_content"] == "weighed bull case"


# ---------------------------------------------------------------------------
# 기능 표 기반 구조화 출력: V4 + reasoner에서는 tool_choice 생략
# ---------------------------------------------------------------------------


def _bound_kwargs(runnable):
    """with_structured_output 결과에서 bind()에 넘겨진 키워드 인자를 추출하는 헬퍼."""
    first = runnable.steps[0] if hasattr(runnable, "steps") else runnable
    return getattr(first, "kwargs", {})


@pytest.mark.unit
class TestStructuredOutputCapabilityDispatch:
    """DeepSeek V4와 reasoner는 tool_choice 파라미터를 거부합니다
    (공식 가이드 api-docs.deepseek.com/guides/tool_calls는 tool_choice 없이
    tools=[...]만 전달함). 기능 표 기반 분기(dispatch)가 해당 모델들에서는
    tool_choice를 생략하고 chat 모델에서는 전송하는지 검증하는 테스트 묶음."""

    class _Sample(BaseModel):
        answer: str

    def _client(self, model):
        return DeepSeekChatOpenAI(
            model=model, api_key="placeholder", base_url="https://api.deepseek.com",
        )

    def test_chat_sends_tool_choice(self):
        """deepseek-chat 모델에는 tool_choice가 전송되는지 검증하는 테스트."""
        bound = self._client("deepseek-chat").with_structured_output(self._Sample)
        assert _bound_kwargs(bound).get("tool_choice") is not None

    def test_reasoner_suppresses_tool_choice(self):
        """deepseek-reasoner에서는 tool_choice가 생략되는지 검증하는 테스트."""
        bound = self._client("deepseek-reasoner").with_structured_output(self._Sample)
        # tool_choice는 아예 없거나 명시적으로 None — 둘 다 langchain의
        # bind_tools가 이 파라미터를 건너뛴다는 유효한 신호입니다.
        assert _bound_kwargs(bound).get("tool_choice") in (None, ...) or \
            "tool_choice" not in _bound_kwargs(bound)

    def test_v4_flash_suppresses_tool_choice(self):
        """deepseek-v4-flash에서도 tool_choice가 생략되는지 검증하는 테스트."""
        bound = self._client("deepseek-v4-flash").with_structured_output(self._Sample)
        assert _bound_kwargs(bound).get("tool_choice") is None or \
            "tool_choice" not in _bound_kwargs(bound)

    def test_v4_pro_suppresses_tool_choice(self):
        """deepseek-v4-pro에서도 tool_choice가 생략되는지 검증하는 테스트."""
        bound = self._client("deepseek-v4-pro").with_structured_output(self._Sample)
        assert _bound_kwargs(bound).get("tool_choice") is None or \
            "tool_choice" not in _bound_kwargs(bound)

    def test_future_v_variant_via_regex(self):
        """미지의 deepseek-v\\d-* ID도 정규식을 통해 V4 특성을 물려받는지 검증하는 테스트 (상위 호환성)."""
        bound = self._client("deepseek-v5-hypothetical").with_structured_output(self._Sample)
        assert _bound_kwargs(bound).get("tool_choice") is None or \
            "tool_choice" not in _bound_kwargs(bound)

    def test_schema_is_still_bound_as_tool(self):
        """tool_choice는 생략되어도 스키마(schema)는 여전히 도구(tool)로 바인딩되는지
        검증하는 테스트 — DeepSeek 공식 도구 호출 예제와 정확히 일치하는 방식입니다."""
        bound = self._client("deepseek-reasoner").with_structured_output(self._Sample)
        kwargs = _bound_kwargs(bound)
        tools = kwargs.get("tools", [])
        assert any(
            t.get("function", {}).get("name") == "_Sample" for t in tools
        ), f"schema not bound as a tool: {tools}"


# ---------------------------------------------------------------------------
# 실제 API: 진짜 DeepSeek 백엔드를 상대로 구조화 출력 왕복(round-trip) 검증
# ---------------------------------------------------------------------------


def _has_real_deepseek_key():
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    return bool(key) and key != "placeholder"


@pytest.mark.integration
@pytest.mark.skipif(
    not _has_real_deepseek_key(),
    reason="DEEPSEEK_API_KEY not set (or placeholder); skipping live API call",
)
class TestDeepSeekLiveStructuredOutput:
    """엔드투엔드(end-to-end): 실제 DeepSeek V4-flash 호출이 타입이 지정된 인스턴스를 반환하는지 검증.

    tool_choice를 생략하는 경로가 이슈 #678에서 보고된 400 오류를 일으키지
    않는지, 그리고 구조화 출력 바인딩이 여전히 Pydantic 인스턴스로
    파싱되는지 확인합니다.
    """

    class _Pick(BaseModel):
        action: str
        confidence: float

    def test_v4_flash_returns_structured_output(self):
        """실제 API 호출로 구조화 출력이 올바른 타입과 값 범위로 반환되는지 검증하는 테스트."""
        client = DeepSeekChatOpenAI(
            model="deepseek-v4-flash",
            api_key=os.environ["DEEPSEEK_API_KEY"],
            base_url="https://api.deepseek.com",
            timeout=60,
        )
        bound = client.with_structured_output(self._Pick)
        result = bound.invoke(
            "Pick BUY or SELL or HOLD for a tech stock with strong earnings. "
            "Confidence is a float between 0 and 1."
        )
        assert isinstance(result, self._Pick)
        assert result.action in {"BUY", "SELL", "HOLD"}
        assert 0.0 <= result.confidence <= 1.0


# ---------------------------------------------------------------------------
# 기반 클래스 격리: NormalizedChatOpenAI에는 DeepSeek 전용 동작이 없어야 함
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBaseClassIsolation:
    """DeepSeek 전용 동작이 기반 클래스로 새어 나가지 않았는지 검증하는 테스트 묶음."""

    def test_normalized_does_not_propagate_reasoning_content(self):
        """범용 클래스인 NormalizedChatOpenAI에는 DeepSeek 전용 동작이 없어야 함을
        검증하는 테스트. 해당 동작은 하위 클래스(subclass)에만 있어야 합니다."""
        assert not hasattr(NormalizedChatOpenAI, "_get_request_payload") or (
            NormalizedChatOpenAI._get_request_payload
            is NormalizedChatOpenAI.__bases__[0]._get_request_payload
        )
