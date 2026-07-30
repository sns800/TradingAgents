# 이 파일은 모든 테스트가 공유하는 pytest 픽스처(fixture) 모음입니다.
# API 키가 없는 CI 환경에서도 테스트가 멈추지 않도록 더미 키를 넣어 주고,
# 전역 설정이 테스트 간에 누출되지 않도록 매 테스트마다 초기화합니다.
"""API 키가 없을 때 CI가 멈추는 것을 방지하는 공용 pytest 픽스처(fixture) 모음."""

import os
from unittest.mock import MagicMock, patch

import pytest


def pytest_configure(config):
    for marker in ("unit", "integration", "smoke"):
        config.addinivalue_line("markers", f"{marker}: {marker}-level tests")


_API_KEY_ENV_VARS = (
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "ANTHROPIC_API_KEY",
    "XAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY",
    "DASHSCOPE_CN_API_KEY",
    "ZHIPU_API_KEY",
    "ZHIPU_CN_API_KEY",
    "MINIMAX_API_KEY",
    "MINIMAX_CN_API_KEY",
    "OPENROUTER_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "ALPHA_VANTAGE_API_KEY",
)


@pytest.fixture(autouse=True)
def _dummy_api_keys(monkeypatch):
    for env_var in _API_KEY_ENV_VARS:
        # .get의 기본값 대신 `or`를 쓰는 이유: 환경 변수가 존재하지만 비어 있는 경우
        # (예: .env.example을 복사한 .env에서 키를 빈 값으로 둔 경우)에도
        # 반드시 자리표시자(placeholder) 값이 들어가야 하기 때문입니다.
        monkeypatch.setenv(env_var, os.environ.get(env_var) or "placeholder")


@pytest.fixture(autouse=True)
def _isolate_config():
    """각 테스트 전후로 dataflows 전역 설정(config)을 초기화하는 픽스처.

    ``set_config``는 병합(merge) 방식이라 오버라이드에 없는 키를 지우지 않습니다.
    따라서 어떤 테스트가 예컨대 ``tool_vendors``를 설정하면 이후 테스트로 누출되어
    라우팅 동작이 실행 순서에 의존하게 됩니다. 이를 막기 위해 전역 설정 객체를
    통째로 교체하여 모든 테스트가 깨끗한 DEFAULT_CONFIG에서 시작하게 합니다.
    """
    import copy

    import tradingagents.dataflows.config as config_module
    import tradingagents.default_config as default_config

    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    yield
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)


@pytest.fixture()
def mock_llm_client():
    client = MagicMock()
    client.get_llm.return_value = MagicMock()
    with patch(
        "tradingagents.llm_clients.factory.create_llm_client",
        return_value=client,
    ):
        yield client
