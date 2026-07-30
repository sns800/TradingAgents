# 이 파일은 Gemini 모델에 사고 수준(thinking_level) 파라미터가 올바르게
# 전달되는지 검증하는 테스트 모음입니다. 모델별 허용 값 차이도 확인합니다.
"""Gemini thinking_level 전달 테스트 (Gemini 3.x).

카탈로그는 Gemini 3.x 전용이며, 문자열 ``thinking_level``을 그대로 받습니다.
Pro는 low/high만 허용하고, Flash는 minimal/medium도 허용합니다 —
Pro가 지원하지 않는 "minimal"은 "low"로 매핑됩니다.
"""

from unittest import mock

import pytest

from tradingagents.llm_clients.google_client import GoogleClient


def _captured_kwargs(model, **kwargs):
    captured = {}
    with mock.patch.object(
        __import__("tradingagents.llm_clients.google_client", fromlist=["x"]),
        "NormalizedChatGoogleGenerativeAI",
        lambda **kw: captured.setdefault("kw", kw),
    ):
        GoogleClient(model, api_key="x", **kwargs).get_llm()
    return captured["kw"]


@pytest.mark.parametrize("level", ["minimal", "low", "medium", "high"])
def test_flash_passes_thinking_level_through(level):
    """Flash 모델은 모든 thinking_level 값을 그대로 전달하는지 검증하는 테스트."""
    kw = _captured_kwargs("gemini-3.5-flash", thinking_level=level)
    assert kw["thinking_level"] == level
    assert "thinking_budget" not in kw  # 2.5 시절 파라미터는 더 이상 없음


def test_pro_remaps_minimal_to_low():
    """Pro 모델에서 "minimal"이 "low"로 재매핑되는지 검증하는 테스트."""
    kw = _captured_kwargs("gemini-3.1-pro-preview", thinking_level="minimal")
    assert kw["thinking_level"] == "low"  # Pro는 "minimal"을 허용하지 않음


def test_pro_keeps_high():
    """Pro 모델에서 "high"는 변환 없이 유지되는지 검증하는 테스트."""
    kw = _captured_kwargs("gemini-3.1-pro-preview", thinking_level="high")
    assert kw["thinking_level"] == "high"


def test_no_thinking_level_is_omitted():
    """thinking_level을 지정하지 않으면 파라미터 자체가 생략되는지 검증하는 테스트."""
    kw = _captured_kwargs("gemini-3.5-flash")
    assert "thinking_level" not in kw
    assert "thinking_budget" not in kw
