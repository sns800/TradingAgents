# =============================================================================
# [모듈 개요 - 초보자용]
# 이 파일은 CLI 실행 중 LLM 사용량 통계를 집계하는 콜백 핸들러(callback handler)를
# 정의합니다. LangChain의 콜백 시스템에 끼워 넣으면, 에이전트들이 LLM을 몇 번
# 호출했는지, 도구(tool)를 몇 번 썼는지, 입력/출력 토큰(token)을 얼마나
# 소비했는지를 자동으로 세어 줍니다. CLI 화면의 통계 표시용으로 쓰입니다.
# =============================================================================

import threading
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage
from langchain_core.outputs import LLMResult


class StatsCallbackHandler(BaseCallbackHandler):
    """LLM 호출 횟수, 도구(tool) 호출 횟수, 토큰(token) 사용량을 추적하는 콜백 핸들러."""

    def __init__(self) -> None:
        super().__init__()
        # 여러 에이전트가 동시에(멀티스레드로) 실행될 수 있으므로,
        # 카운터를 갱신할 때 락(lock)으로 경쟁 상태(race condition)를 방지한다.
        self._lock = threading.Lock()
        self.llm_calls = 0
        self.tool_calls = 0
        self.tokens_in = 0
        self.tokens_out = 0

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        **kwargs: Any,
    ) -> None:
        """LLM이 시작될 때 LLM 호출 카운터를 1 증가시킨다."""
        with self._lock:
            self.llm_calls += 1

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        **kwargs: Any,
    ) -> None:
        """채팅 모델(chat model)이 시작될 때 LLM 호출 카운터를 1 증가시킨다."""
        with self._lock:
            self.llm_calls += 1

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        """LLM 응답에서 토큰 사용량(token usage)을 추출한다."""
        # 응답 구조가 예상과 다르면(비어 있거나 형식이 다르면) 조용히 건너뛴다.
        try:
            generation = response.generations[0][0]
        except (IndexError, TypeError):
            return

        # 응답 메시지가 AIMessage이고 usage_metadata 속성이 있을 때만
        # 토큰 사용량 정보를 꺼낸다(제공자에 따라 없을 수도 있음).
        usage_metadata = None
        if hasattr(generation, "message"):
            message = generation.message
            if isinstance(message, AIMessage) and hasattr(message, "usage_metadata"):
                usage_metadata = message.usage_metadata

        if usage_metadata:
            with self._lock:
                self.tokens_in += usage_metadata.get("input_tokens", 0)
                self.tokens_out += usage_metadata.get("output_tokens", 0)

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        **kwargs: Any,
    ) -> None:
        """도구(tool)가 시작될 때 도구 호출 카운터를 1 증가시킨다."""
        with self._lock:
            self.tool_calls += 1

    def get_stats(self) -> dict[str, Any]:
        """현재까지 집계된 통계를 반환한다."""
        with self._lock:
            return {
                "llm_calls": self.llm_calls,
                "tool_calls": self.tool_calls,
                "tokens_in": self.tokens_in,
                "tokens_out": self.tokens_out,
            }
