# 이 파일은 LLM 제공자(provider) 이름을 API 키 환경 변수 이름으로 매핑하는
# 표준 테이블과, 키가 없을 때 CLI에서 사용자에게 키 입력을 요청하는
# ensure_api_key 헬퍼의 동작을 검증하는 테스트 모음입니다.
"""제공자(provider)->환경 변수 표준 매핑과 CLI 키 입력 프롬프트 헬퍼 테스트."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from tradingagents.llm_clients.api_key_env import PROVIDER_API_KEY_ENV, get_api_key_env

# ---- 매핑 커버리지 검증 -----------------------------------------------------


def test_every_select_llm_provider_choice_has_an_entry():
    """CLI 선택지에 나오는 모든 제공자가 매핑 테이블에 존재하는지 검증하는 테스트.

    select_llm_provider()가 매핑이 모르는 제공자를 화면에 보여 주면 안 됩니다.
    """
    # cli/utils.select_llm_provider의 드롭다운 순서를 그대로 반영하여 두 목록이
    # 항상 일치(lockstep)하도록 합니다. 지역별 키(qwen-cn / minimax-cn / glm-cn)는
    # 2차 지역 선택 프롬프트를 통해 도달하므로 이들도 반드시 포함되어야 합니다.
    expected = {
        "openai", "google", "anthropic", "xai", "deepseek",
        "qwen", "qwen-cn",
        "glm", "glm-cn",
        "minimax", "minimax-cn",
        "openrouter", "azure", "ollama",
    }
    assert expected.issubset(PROVIDER_API_KEY_ENV.keys())


@pytest.mark.parametrize(
    "provider,env_var",
    [
        ("openai",     "OPENAI_API_KEY"),
        ("anthropic",  "ANTHROPIC_API_KEY"),
        ("google",     "GOOGLE_API_KEY"),
        ("azure",      "AZURE_OPENAI_API_KEY"),
        ("xai",        "XAI_API_KEY"),
        ("deepseek",   "DEEPSEEK_API_KEY"),
        ("qwen",       "DASHSCOPE_API_KEY"),
        ("qwen-cn",    "DASHSCOPE_CN_API_KEY"),
        ("glm",        "ZHIPU_API_KEY"),
        ("glm-cn",     "ZHIPU_CN_API_KEY"),
        ("minimax",    "MINIMAX_API_KEY"),
        ("minimax-cn", "MINIMAX_CN_API_KEY"),
        ("openrouter", "OPENROUTER_API_KEY"),
    ],
)
def test_known_providers_resolve(provider, env_var):
    """알려진 각 제공자 이름이 올바른 환경 변수 이름으로 변환되는지 검증하는 테스트."""
    assert get_api_key_env(provider) == env_var


def test_ollama_has_no_key():
    """로컬 실행형인 ollama는 API 키가 필요 없음을 검증하는 테스트."""
    assert get_api_key_env("ollama") is None


def test_unknown_provider_returns_none():
    """알 수 없는 제공자 이름에는 None을 반환하는지 검증하는 테스트."""
    assert get_api_key_env("not-a-real-provider") is None


def test_case_insensitive_lookup():
    """제공자 이름 조회가 대소문자를 구분하지 않는지 검증하는 테스트."""
    assert get_api_key_env("OpenAI") == "OPENAI_API_KEY"
    assert get_api_key_env("QWEN-CN") == "DASHSCOPE_CN_API_KEY"


# ---- ensure_api_key 동작 검증 ---------------------------------------------


@pytest.fixture
def cli_utils(monkeypatch):
    """모듈 수준 상태가 일관되도록 cli.utils를 새로 다시 임포트(reload)하는 픽스처."""
    import importlib

    import cli.utils as cli_utils_module
    return importlib.reload(cli_utils_module)


def test_ensure_api_key_returns_existing(monkeypatch, cli_utils):
    """환경 변수에 키가 이미 있으면 프롬프트 없이 그 값을 반환하는지 검증하는 테스트."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-already-set")
    result = cli_utils.ensure_api_key("openai")
    assert result == "sk-already-set"


def test_ensure_api_key_no_op_for_ollama(monkeypatch, cli_utils):
    """ollama는 키 입력을 요구하지 않고 None을 반환하는지 검증하는 테스트."""
    # 환경 변수가 전혀 없어도 ollama는 프롬프트를 띄우지 않고 None을 반환해야 합니다.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with patch.object(cli_utils, "questionary") as mock_q:
        result = cli_utils.ensure_api_key("ollama")
    assert result is None
    mock_q.password.assert_not_called()


def test_ensure_api_key_unknown_provider_no_prompt(monkeypatch, cli_utils):
    """알 수 없는 제공자에는 프롬프트를 띄우지 않고 None을 반환하는지 검증하는 테스트."""
    with patch.object(cli_utils, "questionary") as mock_q:
        result = cli_utils.ensure_api_key("totally-fake-provider")
    assert result is None
    mock_q.password.assert_not_called()


def test_ensure_api_key_prompts_and_writes_to_env(monkeypatch, tmp_path, cli_utils):
    """키가 없을 때 사용자가 입력한 값이 .env 파일과 os.environ 양쪽에 기록되는지 검증하는 테스트."""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)

    fake_prompt = type("P", (), {"ask": staticmethod(lambda: "sk-deepseek-test")})()
    with patch.object(cli_utils.questionary, "password", return_value=fake_prompt):
        result = cli_utils.ensure_api_key("deepseek")

    assert result == "sk-deepseek-test"
    assert os.environ["DEEPSEEK_API_KEY"] == "sk-deepseek-test"
    env_file = tmp_path / ".env"
    assert env_file.exists()
    assert "DEEPSEEK_API_KEY" in env_file.read_text()
    assert "sk-deepseek-test" in env_file.read_text()


def test_ensure_api_key_user_cancels_returns_none(monkeypatch, tmp_path, cli_utils):
    """사용자가 입력을 취소하면(빈 응답) .env에 아무것도 쓰지 않는지 검증하는 테스트."""
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)

    fake_prompt = type("P", (), {"ask": staticmethod(lambda: None)})()
    with patch.object(cli_utils.questionary, "password", return_value=fake_prompt):
        result = cli_utils.ensure_api_key("xai")

    assert result is None
    assert "XAI_API_KEY" not in os.environ
    # find_dotenv의 디렉터리 탐색 결과에 따라 .env가 있을 수도 없을 수도 있지만,
    # 존재한다면 해당 키가 들어 있으면 안 됩니다.
    env_file = tmp_path / ".env"
    if env_file.exists():
        assert "XAI_API_KEY" not in env_file.read_text()


def test_ensure_api_key_updates_existing_env_file(monkeypatch, tmp_path, cli_utils):
    """기존 .env에 있던 다른 키들이 새 키 기록 시에도 보존되는지 검증하는 테스트."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=sk-existing\nOTHER=value\n")

    fake_prompt = type("P", (), {"ask": staticmethod(lambda: "sk-openrouter-new")})()
    with patch.object(cli_utils.questionary, "password", return_value=fake_prompt):
        cli_utils.ensure_api_key("openrouter")

    content = env_file.read_text()
    assert "OPENAI_API_KEY" in content and "sk-existing" in content
    assert "OTHER=value" in content
    assert "OPENROUTER_API_KEY" in content and "sk-openrouter-new" in content
