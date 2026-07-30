# [모듈 개요] LLM 모델 이름 검증(validation) 로직을 확인하는 테스트.
# 카탈로그에 등록된 모델은 검증을 통과하고, 알 수 없는 모델 이름을 쓰면
# 제공자(provider)에 따라 경고(warning)가 발생하는지 검증한다.

import unittest
import warnings

import pytest

from tradingagents.llm_clients.base_client import BaseLLMClient
from tradingagents.llm_clients.model_catalog import get_known_models
from tradingagents.llm_clients.validators import validate_model


class DummyLLMClient(BaseLLMClient):
    def __init__(self, provider: str, model: str):
        self.provider = provider
        super().__init__(model)

    def get_llm(self):
        self.warn_if_unknown_model()
        return object()

    def validate_model(self) -> bool:
        return validate_model(self.provider, self.model)


@pytest.mark.unit
class ModelValidationTests(unittest.TestCase):
    def test_cli_catalog_models_are_all_validator_approved(self):
        """CLI 모델 카탈로그(catalog)에 실린 모든 모델이 검증기(validator)를 통과하는지 검증하는 테스트."""
        for provider, models in get_known_models().items():
            if provider in ("ollama", "openrouter"):
                continue

            for model in models:
                with self.subTest(provider=provider, model=model):
                    self.assertTrue(validate_model(provider, model))

    def test_unknown_model_emits_warning_for_strict_provider(self):
        """엄격한 제공자(예: openai)에서 알 수 없는 모델 이름을 쓰면 경고가 발생하는지 검증하는 테스트."""
        client = DummyLLMClient("openai", "not-a-real-openai-model")

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            client.get_llm()

        self.assertEqual(len(caught), 1)
        self.assertIn("not-a-real-openai-model", str(caught[0].message))
        self.assertIn("openai", str(caught[0].message))

    def test_openrouter_and_ollama_accept_custom_models_without_warning(self):
        """openrouter와 ollama는 사용자 지정(custom) 모델 이름을 경고 없이 허용하는지 검증하는 테스트."""
        for provider in ("openrouter", "ollama"):
            client = DummyLLMClient(provider, "custom-model-name")

            with self.subTest(provider=provider):
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always")
                    client.get_llm()

                self.assertEqual(caught, [])
