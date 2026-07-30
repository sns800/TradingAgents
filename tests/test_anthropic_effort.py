# 이 파일은 Anthropic(Claude) 모델별로 effort 파라미터를 보낼지 말지 결정하는
# 게이트(gate) 로직을 검증하는 테스트 모음입니다. effort를 지원하지 않는 모델에
# 보내면 API가 400 오류를 내므로, 모델별 지원 여부 판별이 정확해야 합니다.
"""Anthropic effort 파라미터 게이팅(gating) 테스트 (#831).

Haiku(모든 버전)와 Sonnet 4.5는 ``effort`` 파라미터를 받으면 400 오류를
반환합니다. Opus 4.5 이상과 Sonnet 4.6 이상만 이를 허용합니다. 게이트는
모델 계열(family)별 최소 버전을 기준으로 판단하므로, 미래의
``claude-{opus,sonnet}-X-Y`` 릴리스도 코드 수정 없이 자동으로 지원됩니다.
"""

import pytest

from tradingagents.llm_clients import anthropic_client as mod


def _capture_kwargs(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        mod, "NormalizedChatAnthropic",
        lambda **kwargs: captured.setdefault("kwargs", kwargs),
    )
    return captured


@pytest.mark.unit
class TestEffortGate:
    @pytest.mark.parametrize(
        "model",
        [
            "claude-haiku-4-5", "claude-haiku-5-0", "claude-haiku-4-7-preview",
            # Sonnet 4.5 이하는 effort에 400 오류 — Sonnet 4.6 이상만 지원함.
            "claude-sonnet-4-5", "claude-sonnet-4-0",
        ],
    )
    def test_unsupported_models_do_not_receive_effort(self, monkeypatch, model):
        """effort를 지원하지 않는 모델에는 effort 파라미터가 전달되지 않는지 검증하는 테스트."""
        captured = _capture_kwargs(monkeypatch)
        mod.AnthropicClient(model=model, effort="medium", api_key="x").get_llm()
        assert "effort" not in captured["kwargs"]

    @pytest.mark.parametrize(
        "model",
        [
            "claude-opus-4-5", "claude-opus-4-6", "claude-opus-4-7",
            "claude-sonnet-4-6",
        ],
    )
    def test_current_opus_and_sonnet_receive_effort(self, monkeypatch, model):
        """현행 Opus 4.5+와 Sonnet 4.6+ 모델에는 effort가 전달되는지 검증하는 테스트."""
        captured = _capture_kwargs(monkeypatch)
        mod.AnthropicClient(model=model, effort="high", api_key="x").get_llm()
        assert captured["kwargs"]["effort"] == "high"

    @pytest.mark.parametrize(
        "model",
        ["claude-opus-5-0", "claude-opus-4-8", "claude-sonnet-5-0"],
    )
    def test_future_opus_sonnet_inherit_effort_via_pattern(self, monkeypatch, model):
        """미래 버전의 Opus/Sonnet도 코드 수정 없이 effort 지원을 물려받는지 검증하는 테스트 (상위 호환성, forward-compat)."""
        captured = _capture_kwargs(monkeypatch)
        mod.AnthropicClient(model=model, effort="low", api_key="x").get_llm()
        assert captured["kwargs"]["effort"] == "low"

    @pytest.mark.parametrize(
        "model",
        # Claude 5 계열은 한 자리 버전 ID를 사용하며, 모두 effort를 지원합니다.
        ["claude-sonnet-5", "claude-fable-5", "claude-mythos-5"],
    )
    def test_claude_5_family_receives_effort(self, monkeypatch, model):
        """Claude 5 계열 모델에 effort가 전달되는지 검증하는 테스트."""
        captured = _capture_kwargs(monkeypatch)
        mod.AnthropicClient(model=model, effort="high", api_key="x").get_llm()
        assert captured["kwargs"]["effort"] == "high"

    def test_mythos_preview_receives_effort(self, monkeypatch):
        """프리뷰(preview) 접미사가 붙은 mythos 모델에도 effort가 전달되는지 검증하는 테스트."""
        captured = _capture_kwargs(monkeypatch)
        mod.AnthropicClient(
            model="claude-mythos-preview", effort="medium", api_key="x"
        ).get_llm()
        assert captured["kwargs"]["effort"] == "medium"

    def test_unknown_anthropic_model_does_not_receive_effort(self, monkeypatch):
        """알 수 없는 모델에는 effort를 보내지 않는 보수적 기본값을 검증하는 테스트 (400 오류 방지)."""
        captured = _capture_kwargs(monkeypatch)
        mod.AnthropicClient(
            model="claude-experimental-x", effort="medium", api_key="x"
        ).get_llm()
        assert "effort" not in captured["kwargs"]

    def test_other_kwargs_still_forwarded_when_effort_skipped(self, monkeypatch):
        """effort를 생략해도 다른 통과(passthrough) 키워드 인자들은 정상 전달되는지 검증하는 테스트."""
        captured = _capture_kwargs(monkeypatch)
        mod.AnthropicClient(
            model="claude-haiku-4-5",
            effort="medium",
            api_key="placeholder",
            max_tokens=1024,
            timeout=30,
        ).get_llm()
        assert captured["kwargs"]["api_key"] == "placeholder"
        assert captured["kwargs"]["max_tokens"] == 1024
        assert captured["kwargs"]["timeout"] == 30
        assert "effort" not in captured["kwargs"]
