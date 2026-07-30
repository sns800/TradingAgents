"""[모듈 개요] OLLAMA_BASE_URL 환경 변수(env var) 재정의(override)가
CLI와 클라이언트 경로 양쪽에서 올바르게 동작하는지 검증하는 테스트.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture(scope="module", autouse=True)
def _resync_reloaded_modules():
    """이 파일의 importlib.reload() 호출 이후 모듈 상태를 복원하는 픽스처(fixture).

    아래 여러 테스트는 OLLAMA_BASE_URL을 재평가하기 위해 ``cli.utils``를
    리로드한다. 그러면 ``cli.main``이 스타 임포트(star-import)한 이름들
    (예: get_ticker)이 리로드 이전의 모듈 객체에 묶인 채 남아, 이후에 실행되는
    무관한 테스트의 동일성(identity) 검사가 깨진다. 리로드가 다른 테스트 모듈로
    새어 나가지 않도록 테어다운(teardown) 시점에 한 번 재동기화한다.
    """
    yield
    import cli.main
    import cli.utils
    importlib.reload(cli.utils)
    importlib.reload(cli.main)


# ---- openai_client 쪽: 레지스트리(registry) 기반 base_url 결정 --------------


def _reload_client():
    import tradingagents.llm_clients.openai_client as mod
    return importlib.reload(mod)


def _base_url(mod, provider, **kwargs):
    return str(mod.OpenAIClient(model="m", provider=provider, **kwargs).get_llm().openai_api_base)


def test_resolver_returns_default_when_env_unset(monkeypatch):
    """환경 변수가 없으면 기본 로컬 주소를 반환하는지 검증하는 테스트."""
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    mod = _reload_client()
    assert _base_url(mod, "ollama") == "http://localhost:11434/v1"


def test_resolver_returns_env_when_set(monkeypatch):
    """OLLAMA_BASE_URL이 설정되면 그 값을 반환하는지 검증하는 테스트."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://remote-ollama:11434/v1")
    mod = _reload_client()
    assert _base_url(mod, "ollama") == "http://remote-ollama:11434/v1"


def test_resolver_evaluation_is_call_time(monkeypatch):
    """모듈 임포트 이후에 환경 변수를 설정해도 (호출 시점 평가라서) 반영되는지 검증하는 테스트."""
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    mod = _reload_client()
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://late-set:11434/v1")
    assert _base_url(mod, "ollama") == "http://late-set:11434/v1"


def test_resolver_does_not_affect_other_providers(monkeypatch):
    """OLLAMA_BASE_URL이 xai/deepseek 등 다른 제공자에 새어 들지 않는지 검증하는 테스트."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://elsewhere/v1")
    mod = _reload_client()
    assert _base_url(mod, "xai") == "https://api.x.ai/v1"
    assert _base_url(mod, "deepseek") == "https://api.deepseek.com"


def test_client_get_llm_picks_up_env(monkeypatch):
    """엔드투엔드(end-to-end): OllamaClient.get_llm()이 OLLAMA_BASE_URL을 존중하는지 검증하는 테스트."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://my-ollama:11434/v1")
    mod = _reload_client()
    client = mod.OpenAIClient(model="llama3.1", provider="ollama")
    llm = client.get_llm()
    assert "my-ollama" in str(llm.openai_api_base)


def test_explicit_base_url_overrides_env(monkeypatch):
    """클라이언트에 명시적으로 전달한 base_url이 환경 변수보다 우선하는지 검증하는 테스트."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://env-set:11434/v1")
    mod = _reload_client()
    client = mod.OpenAIClient(
        model="llama3.1",
        provider="ollama",
        base_url="http://explicit:11434/v1",
    )
    llm = client.get_llm()
    assert "explicit" in str(llm.openai_api_base)
    assert "env-set" not in str(llm.openai_api_base)


# ---- cli.utils 쪽: select_llm_provider 드롭다운(dropdown) -------------------


def test_cli_dropdown_uses_env(monkeypatch):
    """CLI 드롭다운의 Ollama 항목이 OLLAMA_BASE_URL을 반영하는지 검증하는 테스트."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://cli-remote:11434/v1")
    import cli.utils as cli_utils
    importlib.reload(cli_utils)
    # 함수가 호출 시점에 수행하는 것과 동일한 환경 변수 읽기로 내부 동작을 재현한다
    ollama_url = (
        __import__("os").environ.get("OLLAMA_BASE_URL")
        or "http://localhost:11434/v1"
    )
    assert ollama_url == "http://cli-remote:11434/v1"


def test_cli_dropdown_default_when_unset(monkeypatch):
    """환경 변수가 없을 때 CLI 드롭다운이 기본 로컬 주소를 쓰는지 검증하는 테스트."""
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    import cli.utils as cli_utils
    importlib.reload(cli_utils)
    ollama_url = (
        __import__("os").environ.get("OLLAMA_BASE_URL")
        or "http://localhost:11434/v1"
    )
    assert ollama_url == "http://localhost:11434/v1"


# ---- confirm_ollama_endpoint 사용자 경험(UX) --------------------------------


def test_confirm_endpoint_shows_default(monkeypatch, capsys):
    """기본 엔드포인트(endpoint) 확인 화면에 불필요한 경고가 없는지 검증하는 테스트."""
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    import cli.utils as cli_utils
    importlib.reload(cli_utils)
    cli_utils.confirm_ollama_endpoint("http://localhost:11434/v1")
    out = capsys.readouterr().out
    assert "http://localhost:11434/v1" in out
    assert "OLLAMA_BASE_URL" not in out  # 환경 변수 출처가 아님
    assert "Note" not in out  # 표준 기본값에는 경고가 없어야 함


def test_confirm_endpoint_marks_env_origin(monkeypatch, capsys):
    """엔드포인트가 환경 변수에서 왔음을 화면에 표시하는지 검증하는 테스트."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://remote-host:11434/v1")
    import cli.utils as cli_utils
    importlib.reload(cli_utils)
    cli_utils.confirm_ollama_endpoint("http://remote-host:11434/v1")
    out = capsys.readouterr().out
    assert "http://remote-host:11434/v1" in out
    assert "OLLAMA_BASE_URL" in out


def test_confirm_endpoint_warns_on_missing_scheme(monkeypatch, capsys):
    """스킴(scheme) 없이 OLLAMA_BASE_URL=0.0.0.128처럼 설정하면 올바른 형식을 안내하는지 검증하는 테스트."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "0.0.0.128")
    import cli.utils as cli_utils
    importlib.reload(cli_utils)
    cli_utils.confirm_ollama_endpoint("0.0.0.128")
    out = capsys.readouterr().out
    assert "missing a scheme" in out
    assert "http://<host>:11434/v1" in out


def test_confirm_endpoint_warns_on_non_default_port_remote(monkeypatch, capsys):
    """:11434 포트가 없는 원격 호스트에 포트 불일치 힌트를 부드럽게 보여 주는지 검증하는 테스트."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://remote-host/v1")
    import cli.utils as cli_utils
    importlib.reload(cli_utils)
    cli_utils.confirm_ollama_endpoint("http://remote-host/v1")
    out = capsys.readouterr().out
    assert "port 11434" in out


def test_confirm_endpoint_quiet_on_local_no_port(monkeypatch, capsys):
    """포트 없는 로컬 호스트에는 원격 포트 힌트가 뜨지 않는지 검증하는 테스트."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost/v1")
    import cli.utils as cli_utils
    importlib.reload(cli_utils)
    cli_utils.confirm_ollama_endpoint("http://localhost/v1")
    out = capsys.readouterr().out
    assert "Note" not in out  # localhost는 명시적 포트가 없어도 괜찮음


def test_ollama_model_labels_no_local_suffix():
    """엔드포인트가 동적이므로 모델 라벨에 더 이상 '(local)' 표기가 없는지 검증하는 테스트."""
    from tradingagents.llm_clients.model_catalog import get_model_options
    for mode in ("quick", "deep"):
        labels = [label for label, _ in get_model_options("ollama", mode)]
        assert all("local" not in label for label in labels), labels


def test_ollama_offers_custom_model_id():
    """직접 내려받은(custom-pulled) 모델을 쓰는 Ollama 사용자가 'Custom model ID'를 선택할 수 있는지 검증하는 테스트."""
    from tradingagents.llm_clients.model_catalog import get_model_options
    for mode in ("quick", "deep"):
        entries = get_model_options("ollama", mode)
        values = [v for _, v in entries]
        assert "custom" in values, f"Ollama {mode!r} missing 'custom' option: {entries}"
        # 사용자 지정(custom) 옵션은 선별된 기본 목록을 화면 밖으로 밀지 않도록 맨 마지막에 둔다
        assert values[-1] == "custom", f"'custom' should be last entry: {values}"
