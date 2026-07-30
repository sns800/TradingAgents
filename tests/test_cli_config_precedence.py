# 이 파일은 CLI 설정 우선순위(precedence)를 검증하는 테스트 모음입니다.
# 환경 변수로 지정한 값(토론 라운드 수, 체크포인트 등)이 대화형 프롬프트에서
# 고른 값에 덮어써지지 않고 올바르게 우선 적용되는지 확인합니다.
"""CLI 설정 우선순위(precedence) 테스트 (#976, #977).

토론(debate)/리스크 라운드 수나 체크포인트 플래그를 환경 변수로 명시적으로
오버라이드했다면, 대화형 리서치 깊이(research depth) 선택보다 우선해야 합니다 —
CLI가 환경 변수로 설정된 값을 프롬프트/플래그 기본값으로 되돌려 덮어쓰면 안 됩니다.
"""

from unittest import mock

import pytest

import cli.main as m

# get_user_selections()의 반환값과 같은 형태로 만든 최소한의 선택값 dict.
SELECTIONS = {
    "research_depth": 5,
    "shallow_thinker": "gpt-5.4-mini",
    "deep_thinker": "gpt-5.5",
    "backend_url": None,
    "llm_provider": "openai",
    "google_thinking_level": None,
    "openai_reasoning_effort": None,
    "anthropic_effort": None,
    "output_language": "English",
}


def test_research_depth_sets_both_rounds_without_env(monkeypatch):
    """환경 변수가 없으면 리서치 깊이 선택값이 두 라운드 수를 모두 결정하는지 검증하는 테스트."""
    for var in ("TRADINGAGENTS_MAX_DEBATE_ROUNDS", "TRADINGAGENTS_MAX_RISK_ROUNDS"):
        monkeypatch.delenv(var, raising=False)
    cfg = m._build_run_config(SELECTIONS, checkpoint=None)
    assert cfg["max_debate_rounds"] == 5
    assert cfg["max_risk_discuss_rounds"] == 5


def test_env_round_counts_win_over_selection(monkeypatch):
    """환경 변수로 지정한 라운드 수가 대화형 선택값보다 우선하는지 검증하는 테스트."""
    monkeypatch.setenv("TRADINGAGENTS_MAX_DEBATE_ROUNDS", "2")
    monkeypatch.setenv("TRADINGAGENTS_MAX_RISK_ROUNDS", "4")
    # DEFAULT_CONFIG는 임포트 시점에 이미 환경 변수를 반영하므로, 그 상태를 흉내 냅니다.
    patched = dict(m.DEFAULT_CONFIG, max_debate_rounds=2, max_risk_discuss_rounds=4)
    with mock.patch.object(m, "DEFAULT_CONFIG", patched):
        cfg = m._build_run_config(SELECTIONS, checkpoint=None)
    assert cfg["max_debate_rounds"] == 2  # research_depth=5가 아닌 환경 변수 값
    assert cfg["max_risk_discuss_rounds"] == 4


def test_partial_env_only_overrides_that_count(monkeypatch):
    """일부 환경 변수만 설정하면 해당 라운드 수만 오버라이드되는지 검증하는 테스트."""
    monkeypatch.setenv("TRADINGAGENTS_MAX_DEBATE_ROUNDS", "2")
    monkeypatch.delenv("TRADINGAGENTS_MAX_RISK_ROUNDS", raising=False)
    patched = dict(m.DEFAULT_CONFIG, max_debate_rounds=2)
    with mock.patch.object(m, "DEFAULT_CONFIG", patched):
        cfg = m._build_run_config(SELECTIONS, checkpoint=None)
    assert cfg["max_debate_rounds"] == 2  # 환경 변수가 우선
    assert cfg["max_risk_discuss_rounds"] == 5  # research_depth 값으로 대체됨


def test_checkpoint_none_preserves_env_default():
    """체크포인트 플래그를 지정하지 않으면 환경 변수 기반 기본값이 유지되는지 검증하는 테스트."""
    patched = dict(m.DEFAULT_CONFIG, checkpoint_enabled=True)  # 예: 환경 변수로 활성화된 상태
    with mock.patch.object(m, "DEFAULT_CONFIG", patched):
        cfg = m._build_run_config(SELECTIONS, checkpoint=None)
    assert cfg["checkpoint_enabled"] is True  # False로 덮어써지면 안 됨


@pytest.mark.parametrize("flag", [True, False])
def test_checkpoint_flag_overrides_env(flag):
    """명시적으로 준 CLI 체크포인트 플래그는 환경 변수 값보다 우선하는지 검증하는 테스트."""
    patched = dict(m.DEFAULT_CONFIG, checkpoint_enabled=not flag)
    with mock.patch.object(m, "DEFAULT_CONFIG", patched):
        cfg = m._build_run_config(SELECTIONS, checkpoint=flag)
    assert cfg["checkpoint_enabled"] is flag
