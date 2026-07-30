# 이 파일은 LLM SDK 재시도 횟수(llm_max_retries) 설정을 검증하는 테스트 모음입니다.
# 값 검증·변환과, 각 제공자 클라이언트로의 전달을 확인합니다.
"""설정 가능한 LLM SDK 재시도 한도(retry budget) 테스트 (#1090/#1091).

예전에는 각 제공자 SDK의 max_retries(기본 2)가 노출되지 않아, 일시적인
429(요청 과다) 폭주 한 번으로 멀쩡한 멀티 에이전트 실행 전체가 죽곤 했습니다.
이를 위해 모든 제공자 채팅 클라이언트로 전달되는 선택적(opt-in)
llm_max_retries 옵션이 추가되었습니다.
"""
from __future__ import annotations

import importlib

import pytest

import tradingagents.default_config as default_config_module
from tradingagents.graph.trading_graph import TradingAgentsGraph, _coerce_max_retries

# --- 값 변환(coercion) / 검증 -------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("value,expected", [(0, 0), (2, 2), (10, 10), ("6", 6)])
def test_coerce_accepts_non_negative_ints_and_numeric_strings(value, expected):
    """0 이상의 정수와 숫자 문자열이 올바르게 변환되는지 검증하는 테스트."""
    assert _coerce_max_retries(value) == expected


@pytest.mark.unit
@pytest.mark.parametrize("bad", [-1, "-3"])
def test_coerce_rejects_negative(bad):
    """음수 값은 ValueError로 거부되는지 검증하는 테스트."""
    with pytest.raises(ValueError, match=">= 0"):
        _coerce_max_retries(bad)


@pytest.mark.unit
@pytest.mark.parametrize("bad", [True, False])
def test_coerce_rejects_booleans(bad):
    """불리언(True/False)은 정수처럼 보여도 거부되는지 검증하는 테스트."""
    with pytest.raises(ValueError, match="boolean"):
        _coerce_max_retries(bad)


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["abc", "1.5", None])
def test_coerce_rejects_non_integers(bad):
    """정수가 아닌 값(문자, 소수, None)은 거부되는지 검증하는 테스트."""
    with pytest.raises(ValueError, match="integer"):
        _coerce_max_retries(bad)


# --- 제공자 키워드 인자로의 전달(forwarding) --------------------------------------

def _bare_graph(config):
    g = object.__new__(TradingAgentsGraph)
    g.config = config
    return g


@pytest.mark.unit
def test_not_forwarded_when_unset():
    """설정하지 않으면 max_retries가 전달되지 않는지 검증하는 테스트."""
    kwargs = _bare_graph({"llm_provider": "openai", "llm_max_retries": None})._get_provider_kwargs()
    assert "max_retries" not in kwargs


@pytest.mark.unit
@pytest.mark.parametrize("provider", ["openai", "anthropic", "google"])
def test_forwarded_across_providers(provider):
    """모든 제공자(openai/anthropic/google)에 max_retries가 전달되는지 검증하는 테스트."""
    kwargs = _bare_graph({"llm_provider": provider, "llm_max_retries": 6})._get_provider_kwargs()
    assert kwargs["max_retries"] == 6


@pytest.mark.unit
def test_forwarded_env_string_is_coerced():
    """환경 변수에서 온 문자열 값이 정수로 변환되어 전달되는지 검증하는 테스트."""
    # 환경 변수는 문자열로 도착하므로, 소비 측에서 변환합니다 (temperature와 동일)
    kwargs = _bare_graph({"llm_provider": "openai", "llm_max_retries": "4"})._get_provider_kwargs()
    assert kwargs["max_retries"] == 4


@pytest.mark.unit
def test_invalid_config_value_fails_loudly():
    """잘못된 설정값은 조용히 무시되지 않고 요란하게 실패하는지 검증하는 테스트."""
    with pytest.raises(ValueError):
        _bare_graph({"llm_provider": "openai", "llm_max_retries": -1})._get_provider_kwargs()


# --- 환경 변수 오버레이(overlay) -----------------------------------------------------------

def _reload_with_env(monkeypatch, **overrides):
    for key in list(default_config_module._ENV_OVERRIDES):
        monkeypatch.delenv(key, raising=False)
    for key, val in overrides.items():
        monkeypatch.setenv(key, val)
    return importlib.reload(default_config_module)


@pytest.mark.unit
def test_default_is_none(monkeypatch):
    """llm_max_retries 기본값이 None인지 검증하는 테스트."""
    dc = _reload_with_env(monkeypatch)
    assert dc.DEFAULT_CONFIG["llm_max_retries"] is None


@pytest.mark.unit
def test_env_override_sets_config(monkeypatch):
    """환경 변수로 지정한 재시도 횟수가 설정에 반영되는지 검증하는 테스트."""
    dc = _reload_with_env(monkeypatch, TRADINGAGENTS_LLM_MAX_RETRIES="8")
    # 기본값이 None인 키: 환경 변수 값은 문자열로 도착하며 이후 단계에서 변환됩니다.
    assert dc.DEFAULT_CONFIG["llm_max_retries"] == "8"
    assert _coerce_max_retries(dc.DEFAULT_CONFIG["llm_max_retries"]) == 8
