"""[모듈 개요] Responses API는 순정(native) OpenAI에만 존재하므로, openai 제공자에
사용자 지정 base_url이 설정되면 Chat Completions로 폴백(fallback)해야 함을
검증하는 테스트 (#1024).
"""

from __future__ import annotations

import pytest

from tradingagents.llm_clients.openai_client import (
    OpenAIClient,
    _is_native_openai_base_url,
)


@pytest.mark.unit
class NativeBaseUrlTests:
    def test_unset_is_native(self):
        """base_url이 미설정(None/빈 문자열)이면 순정 OpenAI로 간주하는지 검증하는 테스트."""
        assert _is_native_openai_base_url(None) is True
        assert _is_native_openai_base_url("") is True

    def test_openai_hosts_are_native(self):
        """api.openai.com 호스트는 순정 OpenAI로 판정하는지 검증하는 테스트."""
        assert _is_native_openai_base_url("https://api.openai.com/v1") is True
        assert _is_native_openai_base_url("api.openai.com/v1") is True

    def test_custom_endpoints_are_not_native(self):
        """사용자 지정 엔드포인트(위장 도메인 포함)는 순정으로 판정하지 않는지 검증하는 테스트."""
        assert _is_native_openai_base_url("http://localhost:1234/v1") is False
        assert _is_native_openai_base_url("https://my-gateway.example.com/v1") is False
        assert _is_native_openai_base_url("https://api.openai.com.evil.com/v1") is False


@pytest.mark.unit
class ResponsesApiSelectionTests:
    def test_native_openai_enables_responses_api(self, monkeypatch):
        """순정 OpenAI에서는 Responses API가 활성화되는지 검증하는 테스트."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        llm = OpenAIClient("gpt-5.5", provider="openai").get_llm()
        assert getattr(llm, "use_responses_api", False) is True

    def test_custom_base_url_disables_responses_api(self, monkeypatch):
        """사용자 지정 base_url에서는 Responses API가 비활성화되는지 검증하는 테스트."""
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        llm = OpenAIClient(
            "gpt-5.5", base_url="http://localhost:1234/v1", provider="openai"
        ).get_llm()
        # use_responses_api가 없거나 False여야 클라이언트가 Chat Completions로 통신한다.
        assert getattr(llm, "use_responses_api", False) is False
