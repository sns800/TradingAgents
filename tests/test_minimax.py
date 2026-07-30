"""[모듈 개요] MiniMax LLM 클라이언트(MinimaxChatOpenAI)의 특수 동작을 검증하는 테스트.

MiniMax M2.x 추론(reasoning) 모델이 <think> 블록을 ``message.content``에
섞지 않고 ``reasoning_details``에 넣도록, 서브클래스가 요청에
``reasoning_split=True``를 주입하는지 확인한다.
"""

import os

import pytest
from langchain_core.messages import HumanMessage
from pydantic import BaseModel

from tradingagents.llm_clients.openai_client import MinimaxChatOpenAI


def _client(model: str = "MiniMax-M2.7"):
    os.environ.setdefault("MINIMAX_API_KEY", "placeholder")
    return MinimaxChatOpenAI(
        model=model,
        api_key="placeholder",
        base_url="https://api.minimax.io/v1",
    )


@pytest.mark.unit
class TestMinimaxReasoningSplit:
    def test_reasoning_split_sent_via_extra_body_not_top_level(self):
        """reasoning_split이 최상위(top-level)가 아니라 extra_body로 전송되는지 검증하는 테스트."""
        # 반드시 extra_body에 있어야 하고 최상위에 있으면 안 된다:
        # openai SDK는 최상위 파라미터를 검증하며 reasoning_split 같은
        # 알 수 없는 파라미터를 거부하기 때문 (#826).
        payload = _client()._get_request_payload([HumanMessage(content="hi")])
        assert payload.get("extra_body", {}).get("reasoning_split") is True
        assert "reasoning_split" not in payload  # 최상위에는 절대 없어야 함

    def test_non_reasoning_minimax_does_not_inject_reasoning_split(self):
        """추론 모델이 아닌 MiniMax 모델에는 reasoning_split이 주입되지 않는지 검증하는 테스트.

        Coding Plan / MiniMax-Text-01 등 M2 접두사가 아닌 모델은
        reasoning_split을 (최상위든 extra_body든) 전혀 받으면 안 된다 (#826).
        """
        for model in ("minimax-text-01", "MiniMax-Coding-Plan"):
            payload = _client(model)._get_request_payload(
                [HumanMessage(content="hi")]
            )
            assert "reasoning_split" not in payload
            assert "reasoning_split" not in payload.get("extra_body", {})


@pytest.mark.unit
class TestMinimaxStructuredOutputDispatch:
    """M2.x 모델의 구조화 출력(structured output) 처리 방식을 검증하는 테스트 모음.

    M2.x 모델은 기능 지원 테이블(capability table)을 거쳐 라우팅된다 —
    tool_choice는 억제되지만 스키마(schema)는 여전히 도구(tool)로 바인딩된다.
    """

    class _Pick(BaseModel):
        action: str

    def _bound_kwargs(self, runnable):
        first = runnable.steps[0] if hasattr(runnable, "steps") else runnable
        return getattr(first, "kwargs", {})

    def test_m2_7_suppresses_tool_choice(self):
        """MiniMax-M2.7 모델에서 tool_choice가 억제(미설정)되는지 검증하는 테스트."""
        bound = _client("MiniMax-M2.7").with_structured_output(self._Pick)
        kwargs = self._bound_kwargs(bound)
        assert kwargs.get("tool_choice") is None or "tool_choice" not in kwargs

    def test_m2_7_highspeed_suppresses_tool_choice(self):
        """MiniMax-M2.7-highspeed 모델에서도 tool_choice가 억제되는지 검증하는 테스트."""
        bound = _client("MiniMax-M2.7-highspeed").with_structured_output(self._Pick)
        kwargs = self._bound_kwargs(bound)
        assert kwargs.get("tool_choice") is None or "tool_choice" not in kwargs

    def test_schema_still_bound_as_tool(self):
        """tool_choice를 억제해도 스키마는 여전히 도구(tool)로 바인딩되는지 검증하는 테스트."""
        bound = _client("MiniMax-M2.7").with_structured_output(self._Pick)
        tools = self._bound_kwargs(bound).get("tools", [])
        assert any(
            t.get("function", {}).get("name") == "_Pick" for t in tools
        ), f"schema not bound: {tools}"
