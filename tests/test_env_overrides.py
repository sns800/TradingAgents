# 이 파일은 TRADINGAGENTS_* 환경 변수가 기본 설정(DEFAULT_CONFIG)을
# 올바르게 덮어쓰는지(overlay) 검증하는 테스트 모음입니다.
# 문자열/정수/불리언 변환과 잘못된 값 처리도 함께 확인합니다.
"""TRADINGAGENTS_* 환경 변수의 DEFAULT_CONFIG 오버레이(overlay) 테스트."""

from __future__ import annotations

import importlib

import pytest

import tradingagents.default_config as default_config_module


def _reload_with_env(monkeypatch, **overrides):
    """환경 변수를 설정/해제한 뒤 default_config를 다시 로드해 DEFAULT_CONFIG를 재평가하는 헬퍼."""
    for key in list(default_config_module._ENV_OVERRIDES):
        monkeypatch.delenv(key, raising=False)
    for key, val in overrides.items():
        monkeypatch.setenv(key, val)
    return importlib.reload(default_config_module)


def test_no_env_uses_built_in_defaults(monkeypatch):
    """환경 변수가 없으면 내장 기본값이 그대로 쓰이는지 검증하는 테스트."""
    dc = _reload_with_env(monkeypatch)
    assert dc.DEFAULT_CONFIG["llm_provider"] == "openai"
    assert dc.DEFAULT_CONFIG["deep_think_llm"] == "gpt-5.5"
    assert dc.DEFAULT_CONFIG["quick_think_llm"] == "gpt-5.4-mini"
    assert dc.DEFAULT_CONFIG["backend_url"] is None
    assert dc.DEFAULT_CONFIG["max_debate_rounds"] == 1
    assert dc.DEFAULT_CONFIG["checkpoint_enabled"] is False


def test_string_overrides(monkeypatch):
    """문자열 타입 설정들이 환경 변수 값으로 덮어써지는지 검증하는 테스트."""
    dc = _reload_with_env(
        monkeypatch,
        TRADINGAGENTS_LLM_PROVIDER="google",
        TRADINGAGENTS_DEEP_THINK_LLM="gemini-3-pro-preview",
        TRADINGAGENTS_QUICK_THINK_LLM="gemini-3-flash-preview",
        TRADINGAGENTS_LLM_BACKEND_URL="https://example.invalid/v1",
        TRADINGAGENTS_OUTPUT_LANGUAGE="Chinese",
    )
    assert dc.DEFAULT_CONFIG["llm_provider"] == "google"
    assert dc.DEFAULT_CONFIG["deep_think_llm"] == "gemini-3-pro-preview"
    assert dc.DEFAULT_CONFIG["quick_think_llm"] == "gemini-3-flash-preview"
    assert dc.DEFAULT_CONFIG["backend_url"] == "https://example.invalid/v1"
    assert dc.DEFAULT_CONFIG["output_language"] == "Chinese"


def test_int_coercion(monkeypatch):
    """숫자 문자열 환경 변수가 정수(int)로 변환되는지 검증하는 테스트."""
    dc = _reload_with_env(
        monkeypatch,
        TRADINGAGENTS_MAX_DEBATE_ROUNDS="3",
        TRADINGAGENTS_MAX_RISK_ROUNDS="2",
    )
    assert dc.DEFAULT_CONFIG["max_debate_rounds"] == 3
    assert isinstance(dc.DEFAULT_CONFIG["max_debate_rounds"], int)
    assert dc.DEFAULT_CONFIG["max_risk_discuss_rounds"] == 2
    assert isinstance(dc.DEFAULT_CONFIG["max_risk_discuss_rounds"], int)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("true", True), ("True", True), ("1", True), ("yes", True), ("on", True),
        ("false", False), ("False", False), ("0", False), ("no", False), ("off", False),
    ],
)
def test_bool_coercion(monkeypatch, raw, expected):
    """다양한 표기(true/1/yes/on 등)의 불리언 환경 변수가 올바르게 변환되는지 검증하는 테스트."""
    dc = _reload_with_env(monkeypatch, TRADINGAGENTS_CHECKPOINT_ENABLED=raw)
    assert dc.DEFAULT_CONFIG["checkpoint_enabled"] is expected


def test_holding_days_default_and_override(monkeypatch):
    """holding_days가 기본값 5를 갖고 TRADINGAGENTS_HOLDING_DAYS로 덮어써지는지 검증하는 테스트."""
    dc = _reload_with_env(monkeypatch)
    assert dc.DEFAULT_CONFIG["holding_days"] == 5
    dc = _reload_with_env(monkeypatch, TRADINGAGENTS_HOLDING_DAYS="10")
    assert dc.DEFAULT_CONFIG["holding_days"] == 10
    assert isinstance(dc.DEFAULT_CONFIG["holding_days"], int)


def test_memory_log_max_entries_finite_default(monkeypatch):
    """메모리 로그 로테이션 상한 기본값이 무한(None)이 아닌 유한값(200)인지 검증하는 테스트."""
    dc = _reload_with_env(monkeypatch)
    assert dc.DEFAULT_CONFIG["memory_log_max_entries"] == 200


def test_reasoning_thinking_overrides(monkeypatch):
    """제공자별 추론/사고(reasoning/thinking) 옵션이 환경 변수로 설정 가능한지 검증하는 테스트 (비대화형 실행용)."""
    dc = _reload_with_env(
        monkeypatch,
        TRADINGAGENTS_OPENAI_REASONING_EFFORT="high",
        TRADINGAGENTS_GOOGLE_THINKING_LEVEL="minimal",
        TRADINGAGENTS_ANTHROPIC_EFFORT="low",
    )
    assert dc.DEFAULT_CONFIG["openai_reasoning_effort"] == "high"
    assert dc.DEFAULT_CONFIG["google_thinking_level"] == "minimal"
    assert dc.DEFAULT_CONFIG["anthropic_effort"] == "low"


def test_reasoning_effort_defaults_to_none(monkeypatch):
    """설정하지 않은 추론/사고 옵션은 None으로 남아 각 제공자의 자체 기본값이 쓰이는지 검증하는 테스트."""
    dc = _reload_with_env(monkeypatch)
    assert dc.DEFAULT_CONFIG["openai_reasoning_effort"] is None
    assert dc.DEFAULT_CONFIG["google_thinking_level"] is None
    assert dc.DEFAULT_CONFIG["anthropic_effort"] is None


def test_empty_env_value_is_passthrough(monkeypatch):
    """빈 값의 TRADINGAGENTS_* 환경 변수는 내장 기본값을 덮어쓰지 않는지 검증하는 테스트."""
    dc = _reload_with_env(
        monkeypatch,
        TRADINGAGENTS_LLM_PROVIDER="",
        TRADINGAGENTS_MAX_DEBATE_ROUNDS="",
    )
    assert dc.DEFAULT_CONFIG["llm_provider"] == "openai"
    assert dc.DEFAULT_CONFIG["max_debate_rounds"] == 1


def test_invalid_int_raises(monkeypatch):
    """숫자가 아닌 값은 조용히 잘못 설정되는 대신 임포트 시점에 ValueError를 내는지 검증하는 테스트."""
    monkeypatch.setenv("TRADINGAGENTS_MAX_DEBATE_ROUNDS", "not-a-number")
    with pytest.raises(ValueError, match="TRADINGAGENTS_MAX_DEBATE_ROUNDS"):
        importlib.reload(default_config_module)
    # 같은 프로세스의 이후 테스트를 위해 모듈 상태를 복원
    monkeypatch.delenv("TRADINGAGENTS_MAX_DEBATE_ROUNDS", raising=False)
    importlib.reload(default_config_module)


@pytest.mark.parametrize("bad", ["treu", "flase", "maybe", "2", "enabled"])
def test_invalid_bool_raises(monkeypatch, bad):
    """철자가 틀린 불리언 값은 조용히 False가 되는 대신 (정수처럼) 요란하게 실패하는지 검증하는 테스트."""
    monkeypatch.setenv("TRADINGAGENTS_CHECKPOINT_ENABLED", bad)
    with pytest.raises(ValueError, match="TRADINGAGENTS_CHECKPOINT_ENABLED"):
        importlib.reload(default_config_module)
    monkeypatch.delenv("TRADINGAGENTS_CHECKPOINT_ENABLED", raising=False)
    importlib.reload(default_config_module)


def test_unknown_env_var_is_ignored(monkeypatch):
    """_ENV_OVERRIDES에 없는 환경 변수는 DEFAULT_CONFIG에 스며들지 않는지 검증하는 테스트."""
    dc = _reload_with_env(
        monkeypatch,
        TRADINGAGENTS_NONEXISTENT_KEY="oops",
    )
    assert "nonexistent_key" not in dc.DEFAULT_CONFIG
