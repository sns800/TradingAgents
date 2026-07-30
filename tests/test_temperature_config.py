"""[모듈 개요] 설정 가능한 샘플링 온도(temperature)를 검증하는 테스트 (#178/#168).

온도는 제공자 공통 조절 값(knob)이다: 설정되면 하위 채팅 클라이언트까지
전달돼야 하고, 설정되지 않으면 제공자 자체 기본값이 유지돼야 한다.
"""

import importlib

import pytest

from tradingagents.llm_clients.factory import create_llm_client


@pytest.mark.unit
class TestTemperatureForwarding:
    @pytest.mark.parametrize(
        "provider,model",
        [
            # gpt-4.1은 의도적으로 고른 비추론(non-reasoning) 모델이다: GPT-5
            # 계열은 추론 모델이라 온도를 올바르게 제거하므로(참고:
            # test_openai_reasoning_effort), 전달 여부는 gpt-4.1로 테스트한다.
            ("openai", "gpt-4.1"),
            ("anthropic", "claude-sonnet-5"),
            ("google", "gemini-3.5-flash"),
            ("deepseek", "deepseek-chat"),
        ],
    )
    def test_temperature_reaches_client_when_set(self, provider, model):
        """설정한 온도 값이 각 제공자의 채팅 클라이언트까지 전달되는지 검증하는 테스트."""
        llm = create_llm_client(
            provider=provider, model=model, temperature=0.0, api_key="placeholder"
        ).get_llm()
        assert llm.temperature == 0.0

    def test_temperature_omitted_leaves_provider_default(self):
        """온도를 지정하지 않으면 제공자 기본값이 유지되는지 검증하는 테스트."""
        # 온도를 전달하지 않으면 특정 값으로 강제되면 안 된다.
        llm = create_llm_client(
            provider="openai", model="gpt-4.1", api_key="placeholder"
        ).get_llm()
        # langchain의 기본값은 0.0이 아니라 미설정/None이다
        assert llm.temperature is None


@pytest.mark.unit
class TestTemperatureEnvOverlay:
    def test_env_sets_temperature(self, monkeypatch):
        """TRADINGAGENTS_TEMPERATURE 환경 변수가 설정 값에 반영되는지 검증하는 테스트."""
        import tradingagents.default_config as dc
        monkeypatch.setenv("TRADINGAGENTS_TEMPERATURE", "0.2")
        importlib.reload(dc)
        # 설정(config)에 저장됨 (환경 변수의 문자열도 무방; float()로 소비된다).
        assert dc.DEFAULT_CONFIG["temperature"] in ("0.2", 0.2)
        assert float(dc.DEFAULT_CONFIG["temperature"]) == 0.2
        monkeypatch.delenv("TRADINGAGENTS_TEMPERATURE", raising=False)
        importlib.reload(dc)

    def test_default_temperature_is_none(self, monkeypatch):
        """환경 변수가 없으면 기본 온도가 None인지 검증하는 테스트."""
        import tradingagents.default_config as dc
        monkeypatch.delenv("TRADINGAGENTS_TEMPERATURE", raising=False)
        importlib.reload(dc)
        assert dc.DEFAULT_CONFIG["temperature"] is None


@pytest.mark.unit
class TestProviderKwargsTemperature:
    """_get_provider_kwargs가 온도를 실수(float)로 변환해 전달하거나 생략하는지 검증하는 테스트 모음."""

    def _kwargs_for(self, temperature):
        from tradingagents.graph.trading_graph import TradingAgentsGraph
        # 전체 그래프를 생성하지 않고 메서드만 호출한다.
        graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
        graph.config = {"llm_provider": "openai", "temperature": temperature}
        return TradingAgentsGraph._get_provider_kwargs(graph)

    def test_float_string_coerced(self):
        """문자열 형태의 온도 값이 실수로 변환되는지 검증하는 테스트."""
        assert self._kwargs_for("0.3")["temperature"] == 0.3

    def test_float_passthrough(self):
        """실수 값(0.0 포함)이 그대로 전달되는지 검증하는 테스트."""
        assert self._kwargs_for(0.0)["temperature"] == 0.0

    def test_none_omitted(self):
        """온도가 None이면 인자에서 생략되는지 검증하는 테스트."""
        assert "temperature" not in self._kwargs_for(None)

    def test_empty_string_omitted(self):
        """온도가 빈 문자열이면 인자에서 생략되는지 검증하는 테스트."""
        assert "temperature" not in self._kwargs_for("")
