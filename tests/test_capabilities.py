# 이 파일은 모델별 LLM 기능 지원 표(capability table)를 검증하는 테스트 모음입니다.
# 각 모델이 tool_choice, JSON 모드 등 특정 기능을 지원하는지 여부가
# 정확히 조회되는지 확인합니다.
"""LLM 기능 지원 표(capability table)의 단위(unit) 테스트."""

from dataclasses import FrozenInstanceError

import pytest

from tradingagents.llm_clients.capabilities import (
    get_capabilities,
)


@pytest.mark.unit
class TestExactIdMatches:
    """모델 ID가 표에 정확히 일치(exact match)할 때의 기능 조회를 검증하는 테스트 묶음."""

    def test_deepseek_chat_supports_tool_choice(self):
        """deepseek-chat 모델은 tool_choice를 지원함을 검증하는 테스트."""
        caps = get_capabilities("deepseek-chat")
        assert caps.supports_tool_choice is True

    def test_deepseek_reasoner_rejects_tool_choice(self):
        """deepseek-reasoner는 tool_choice를 거부하고 추론 내용 왕복(roundtrip)이 필요함을 검증하는 테스트."""
        caps = get_capabilities("deepseek-reasoner")
        assert caps.supports_tool_choice is False
        assert caps.requires_reasoning_content_roundtrip is True

    def test_deepseek_v4_flash_rejects_tool_choice(self):
        """deepseek-v4-flash도 tool_choice를 거부함을 검증하는 테스트."""
        caps = get_capabilities("deepseek-v4-flash")
        assert caps.supports_tool_choice is False
        assert caps.requires_reasoning_content_roundtrip is True

    def test_deepseek_v4_pro_rejects_tool_choice(self):
        """deepseek-v4-pro도 tool_choice를 거부함을 검증하는 테스트."""
        caps = get_capabilities("deepseek-v4-pro")
        assert caps.supports_tool_choice is False
        assert caps.requires_reasoning_content_roundtrip is True


@pytest.mark.unit
class TestPatternMatches:
    """상위 호환(forward-compat) 정규식 패턴이 미지의 DeepSeek·MiniMax 변형 모델을 잡아내는지 검증하는 테스트 묶음."""

    def test_future_deepseek_v5_inherits_thinking_quirks(self):
        """미래의 deepseek-v5 계열도 추론(thinking) 모델 특성을 물려받는지 검증하는 테스트."""
        caps = get_capabilities("deepseek-v5-flash")
        assert caps.supports_tool_choice is False
        assert caps.requires_reasoning_content_roundtrip is True

    def test_future_deepseek_v9_inherits_thinking_quirks(self):
        """더 먼 미래의 deepseek-v9 계열도 같은 특성을 물려받는지 검증하는 테스트."""
        caps = get_capabilities("deepseek-v9-anything")
        assert caps.supports_tool_choice is False

    def test_reasoner_variant_inherits_thinking_quirks(self):
        """reasoner 변형 모델명도 추론 모델 특성을 물려받는지 검증하는 테스트."""
        caps = get_capabilities("deepseek-reasoner-pro")
        assert caps.supports_tool_choice is False

    def test_minimax_m3_inherits_thinking_quirks(self):
        """MiniMax-M3도 패턴 매칭으로 추론 모델 특성을 물려받는지 검증하는 테스트."""
        caps = get_capabilities("MiniMax-M3")
        assert caps.supports_tool_choice is False

    def test_future_minimax_m4_highspeed_inherits_thinking_quirks(self):
        """미래의 MiniMax-M4 변형도 같은 특성을 물려받는지 검증하는 테스트."""
        caps = get_capabilities("MiniMax-M4-highspeed")
        assert caps.supports_tool_choice is False


@pytest.mark.unit
class TestMinimaxExactMatches:
    """MiniMax M2.x 모델은 langchain이 보내는 함수 명세 dict 형태의 tool_choice를
    거부함을 검증하는 테스트 묶음 (공식 API 열거값은 none/auto만 허용)."""

    def test_m2_7_rejects_tool_choice(self):
        """MiniMax-M2.7이 tool_choice와 JSON 모드를 모두 거부함을 검증하는 테스트."""
        caps = get_capabilities("MiniMax-M2.7")
        assert caps.supports_tool_choice is False
        assert caps.supports_json_mode is False  # json_object는 MiniMax-Text-01만 지원함

    def test_m2_7_highspeed_rejects_tool_choice(self):
        """highspeed 변형도 동일하게 tool_choice를 거부함을 검증하는 테스트."""
        assert get_capabilities("MiniMax-M2.7-highspeed").supports_tool_choice is False

    def test_m2_1_rejects_tool_choice(self):
        """MiniMax-M2.1도 tool_choice를 거부함을 검증하는 테스트."""
        assert get_capabilities("MiniMax-M2.1").supports_tool_choice is False

    def test_m2_base_rejects_tool_choice(self):
        """기본 MiniMax-M2도 tool_choice를 거부함을 검증하는 테스트."""
        assert get_capabilities("MiniMax-M2").supports_tool_choice is False

    def test_m2_x_requires_reasoning_split(self):
        """M2.x 추론 모델에 reasoning_split이 필요함을 검증하는 테스트."""
        # M2.x 추론 모델은 <think> 블록이 content가 아닌 reasoning_details에
        # 들어가도록 reasoning_split=True가 필요합니다 (#826).
        for model in ("MiniMax-M2.7", "MiniMax-M2.5-highspeed", "MiniMax-M2"):
            assert get_capabilities(model).requires_reasoning_split is True

    def test_future_m3_inherits_reasoning_split(self):
        """미래의 M3 변형도 reasoning_split 요구 사항을 물려받는지 검증하는 테스트."""
        assert get_capabilities("MiniMax-M3-highspeed").requires_reasoning_split is True

    def test_non_reasoning_minimax_does_not_get_reasoning_split(self):
        """추론 모델이 아닌 MiniMax 모델에는 reasoning_split을 적용하지 않는지 검증하는 테스트."""
        # Coding Plan, MiniMax-Text-01, 그리고 M2 접두사가 없는 모든 MiniMax
        # 모델은 openai SDK의 엄격한 검증 때문에 reasoning_split 키워드 인자를
        # 거부합니다 (#826). 기본 기능 설정에서는 비활성화되어 있습니다.
        for model in ("minimax-text-01", "MiniMax-Coding-Plan", "abab6.5-chat"):
            assert get_capabilities(model).requires_reasoning_split is False


@pytest.mark.unit
class TestDefault:
    """미지의 모델이나 DeepSeek 이외 모델은 관대한(permissive) 기본값을 받는지 검증하는 테스트 묶음."""

    def test_gpt_default(self):
        """GPT 모델이 기본값(tool_choice 지원, function_calling 선호)을 받는지 검증하는 테스트."""
        caps = get_capabilities("gpt-4.1")
        assert caps.supports_tool_choice is True
        assert caps.preferred_structured_method == "function_calling"

    def test_grok_default(self):
        """Grok 모델도 기본값으로 tool_choice를 지원하는지 검증하는 테스트."""
        caps = get_capabilities("grok-4-0709")
        assert caps.supports_tool_choice is True

    def test_unknown_model_default(self):
        """완전히 알 수 없는 모델 ID에도 기본값이 적용되는지 검증하는 테스트."""
        caps = get_capabilities("totally-made-up-model-id")
        assert caps.supports_tool_choice is True

    def test_exact_match_precedes_pattern(self):
        """정확 일치 항목이 패턴보다 우선하는지 검증하는 테스트 — deepseek-chat이 v\\d 정규식에 걸리면 안 됩니다."""
        caps = get_capabilities("deepseek-chat")
        assert caps.supports_tool_choice is True


@pytest.mark.unit
def test_capabilities_dataclass_is_frozen():
    """기능 표의 행(row)이 불변(immutable)이라 안전하게 공유할 수 있는지 검증하는 테스트."""
    caps = get_capabilities("deepseek-chat")
    with pytest.raises(FrozenInstanceError):
        caps.supports_tool_choice = False  # type: ignore[misc]
