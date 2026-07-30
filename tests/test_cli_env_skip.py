# 이 파일은 환경 변수로 설정을 미리 지정했을 때 CLI가 해당 대화형 프롬프트를
# 건너뛰고 그 값을 그대로 사용하는지 검증하는 테스트 모음입니다.
"""환경 변수 기반 CLI 동작 테스트 (#897, #873).

설정 계층의 오버라이드(TRADINGAGENTS_* -> DEFAULT_CONFIG)는
test_env_overrides.py에서 다룹니다. 이 테스트들은 CLI 계층을 다룹니다:
환경 변수로 설정된 제공자/모델/언어는 해당 대화형 프롬프트를 건너뛰고
그 값을 사용해야 합니다.
"""

import os
import unittest
from unittest import mock

import pytest


@pytest.mark.unit
class TestProviderDefaultUrl(unittest.TestCase):
    """제공자별 기본 백엔드 URL 조회 헬퍼를 검증하는 테스트 묶음."""

    def test_known_providers_resolve(self):
        """알려진 제공자 이름이 올바른 기본 URL로 변환되는지 검증하는 테스트."""
        from cli.utils import provider_default_url
        self.assertEqual(provider_default_url("openai"), "https://api.openai.com/v1")
        self.assertEqual(provider_default_url("DeepSeek"), "https://api.deepseek.com")
        self.assertIsNone(provider_default_url("google"))  # SDK 기본값을 사용함

    def test_unknown_provider_returns_none(self):
        """알 수 없는 제공자에는 None을 반환하는지 검증하는 테스트."""
        from cli.utils import provider_default_url
        self.assertIsNone(provider_default_url("not-a-provider"))

    def test_ollama_honors_base_url_env(self):
        """ollama가 OLLAMA_BASE_URL 환경 변수를 존중하는지 검증하는 테스트."""
        from cli.utils import provider_default_url
        with mock.patch.dict(os.environ, {"OLLAMA_BASE_URL": "http://host:1234/v1"}):
            self.assertEqual(provider_default_url("ollama"), "http://host:1234/v1")


@pytest.mark.unit
class TestCliSkipsPromptsFromEnv(unittest.TestCase):
    """환경 변수로 LLM 설정이 지정되면 관련 프롬프트를 건너뛰는지 검증하는 테스트 묶음."""

    def test_env_config_skips_llm_prompts(self):
        """환경 변수로 지정된 제공자/모델/언어에 대해 LLM 선택 프롬프트가 뜨지 않는지 검증하는 테스트."""
        import cli.main as m

        env = {
            "TRADINGAGENTS_LLM_PROVIDER": "openai",
            "TRADINGAGENTS_DEEP_THINK_LLM": "kimi-k2.5",
            "TRADINGAGENTS_QUICK_THINK_LLM": "deepseek-v4-pro",
            "TRADINGAGENTS_LLM_BACKEND_URL": "https://opencode.ai/zen/go/v1",
            "TRADINGAGENTS_OUTPUT_LANGUAGE": "Japanese",
        }
        fake_cfg = dict(m.DEFAULT_CONFIG)
        fake_cfg.update({
            "llm_provider": "openai",
            "backend_url": "https://opencode.ai/zen/go/v1",
            "quick_think_llm": "deepseek-v4-pro",
            "deep_think_llm": "kimi-k2.5",
            "output_language": "Japanese",
        })

        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(m, "DEFAULT_CONFIG", fake_cfg), \
             mock.patch.object(m, "fetch_announcements", return_value=None), \
             mock.patch.object(m, "display_announcements"), \
             mock.patch.object(m, "get_ticker", return_value="AAPL"), \
             mock.patch.object(m, "get_analysis_date", return_value="2026-05-29"), \
             mock.patch.object(m, "select_analysts", return_value=[]), \
             mock.patch.object(m, "select_research_depth", return_value=1), \
             mock.patch.object(m, "ensure_api_key") as ensure_key, \
             mock.patch.object(m, "select_llm_provider") as prompt_provider, \
             mock.patch.object(m, "ask_output_language") as prompt_lang, \
             mock.patch.object(m, "select_shallow_thinking_agent") as prompt_quick, \
             mock.patch.object(m, "select_deep_thinking_agent") as prompt_deep:
            sel = m.get_user_selections()

        # LLM 선택 프롬프트가 하나도 표시되지 않았어야 합니다.
        prompt_provider.assert_not_called()
        prompt_lang.assert_not_called()
        prompt_quick.assert_not_called()
        prompt_deep.assert_not_called()
        # 환경 변수로 설정된 제공자라도 API 키 확인은 여전히 수행됩니다.
        ensure_key.assert_called_once()

        # 환경 변수 값이 반환된 선택값에 그대로 흘러 들어갑니다.
        self.assertEqual(sel["llm_provider"], "openai")
        self.assertEqual(sel["backend_url"], "https://opencode.ai/zen/go/v1")
        self.assertEqual(sel["shallow_thinker"], "deepseek-v4-pro")
        self.assertEqual(sel["deep_thinker"], "kimi-k2.5")
        self.assertEqual(sel["output_language"], "Japanese")


@pytest.mark.unit
class TestResearchDepthSkippedFromEnv(unittest.TestCase):
    """라운드 수 환경 변수가 모두 지정되면 리서치 깊이 프롬프트를 건너뛰는지 검증하는 테스트 묶음."""

    def test_both_round_envs_skip_depth_prompt(self):
        """두 라운드 환경 변수가 모두 설정되면 깊이 선택 프롬프트가 생략되는지 검증하는 테스트."""
        import cli.main as m

        env = {
            "TRADINGAGENTS_MAX_DEBATE_ROUNDS": "2",
            "TRADINGAGENTS_MAX_RISK_ROUNDS": "4",
        }
        fake_cfg = dict(m.DEFAULT_CONFIG)
        fake_cfg.update({"max_debate_rounds": 2, "max_risk_discuss_rounds": 4})

        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(m, "DEFAULT_CONFIG", fake_cfg), \
             mock.patch.object(m, "fetch_announcements", return_value=None), \
             mock.patch.object(m, "display_announcements"), \
             mock.patch.object(m, "get_ticker", return_value="AAPL"), \
             mock.patch.object(m, "get_analysis_date", return_value="2026-05-29"), \
             mock.patch.object(m, "select_analysts", return_value=[]), \
             mock.patch.object(m, "select_research_depth") as prompt_depth, \
             mock.patch.object(m, "ensure_api_key"), \
             mock.patch.object(m, "select_llm_provider", return_value=("openai", None)), \
             mock.patch.object(m, "ask_output_language", return_value="English"), \
             mock.patch.object(m, "select_shallow_thinking_agent", return_value="gpt-5.4-mini"), \
             mock.patch.object(m, "select_deep_thinking_agent", return_value="gpt-5.5"), \
             mock.patch.object(m, "ask_openai_reasoning_effort", return_value=None):
            sel = m.get_user_selections()

        # 리서치 깊이 프롬프트는 생략되고, 값은 환경 변수 설정에서 옵니다.
        prompt_depth.assert_not_called()
        self.assertEqual(sel["research_depth"], 2)


@pytest.mark.unit
class TestReasoningEffortSkippedFromEnv(unittest.TestCase):
    """추론 강도(reasoning effort) 환경 변수가 있으면 관련 프롬프트를 건너뛰는지 검증하는 테스트 묶음."""

    def test_effort_env_skips_step8_prompt(self):
        """effort 환경 변수가 설정되면 8단계 프롬프트가 생략되는지 검증하는 테스트."""
        import cli.main as m

        env = {"TRADINGAGENTS_OPENAI_REASONING_EFFORT": "high"}
        fake_cfg = dict(m.DEFAULT_CONFIG)
        fake_cfg.update({"openai_reasoning_effort": "high"})

        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(m, "DEFAULT_CONFIG", fake_cfg), \
             mock.patch.object(m, "fetch_announcements", return_value=None), \
             mock.patch.object(m, "display_announcements"), \
             mock.patch.object(m, "get_ticker", return_value="AAPL"), \
             mock.patch.object(m, "get_analysis_date", return_value="2026-05-29"), \
             mock.patch.object(m, "select_analysts", return_value=[]), \
             mock.patch.object(m, "select_research_depth", return_value=1), \
             mock.patch.object(m, "ensure_api_key"), \
             mock.patch.object(m, "select_llm_provider", return_value=("openai", None)), \
             mock.patch.object(m, "ask_output_language", return_value="English"), \
             mock.patch.object(m, "select_shallow_thinking_agent", return_value="gpt-5.4-mini"), \
             mock.patch.object(m, "select_deep_thinking_agent", return_value="gpt-5.5"), \
             mock.patch.object(m, "ask_openai_reasoning_effort") as prompt_effort:
            sel = m.get_user_selections()

        # 추론 강도 프롬프트는 생략되고, 값은 환경 변수 설정에서 옵니다.
        prompt_effort.assert_not_called()
        self.assertEqual(sel["openai_reasoning_effort"], "high")


if __name__ == "__main__":
    unittest.main()
