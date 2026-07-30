"""[모듈 개요] OpenAI ``reasoning_effort`` 파라미터가 추론(reasoning) 모델에만
전달되는지 검증하는 테스트.

추론 모델이 아닌 OpenAI 모델(gpt-4.1, gpt-4o, ...)은 "Unsupported parameter:
'reasoning.effort'"와 함께 400 오류를 낸다. 클라이언트는 이런 모델에 대해
해당 키워드 인자(kwarg)를 전달해 실행을 중단시키는 대신 제거해야 한다.
GPT-5 계열과 o 시리즈는 이 파라미터를 허용한다.
"""

import pytest

from tradingagents.llm_clients.openai_client import (
    OpenAIClient,
    _supports_reasoning_effort,
)


@pytest.mark.parametrize(
    "model,expected",
    [
        ("gpt-5.5", True), ("gpt-5.4", True), ("gpt-5.4-mini", True),
        ("gpt-5.5-pro", True), ("o1", True), ("o3-mini", True),
        ("gpt-4.1", False), ("gpt-4o", False), ("gpt-4o-mini", False),
        ("gpt-3.5-turbo", False),
    ],
)
def test_supports_reasoning_effort(model, expected):
    """모델별로 reasoning_effort 지원 여부를 올바르게 판별하는지 검증하는 테스트."""
    assert _supports_reasoning_effort(model) is expected


def _effort_on(model, monkeypatch):
    # 가짜 키를 쓰면 get_llm()이 네트워크 호출 없이 클라이언트를 생성할 수 있다.
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    llm = OpenAIClient(model, provider="openai", reasoning_effort="low").get_llm()
    return getattr(llm, "reasoning_effort", None)


def test_reasoning_model_receives_effort(monkeypatch):
    """추론 모델에는 reasoning_effort가 그대로 전달되는지 검증하는 테스트."""
    assert _effort_on("gpt-5.4-mini", monkeypatch) == "low"


def test_non_reasoning_model_drops_effort(monkeypatch):
    """비추론 모델에서는 reasoning_effort가 제거되는지 검증하는 테스트."""
    # gpt-4.1은 reasoning_effort가 있으면 400 오류를 내므로 반드시 제거해야 한다.
    assert _effort_on("gpt-4.1", monkeypatch) is None
