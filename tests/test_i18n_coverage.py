# 이 파일은 보고서를 생성하는 모든 에이전트가 설정된 출력 언어(output language)
# 지시를 적용하는지 검증하는 테스트 모음입니다. 다국어 보고서가 언어 혼용 없이
# 온전히 지정 언어로 나오도록 보장합니다.
"""보고서를 생성하는 모든 에이전트는 설정된 출력 언어를 적용해야 함 (#740/#801).

영어가 아닌 언어로 실행하면 여러 언어가 섞이지 않은, 완전히 현지화된 보고서가
나와야 합니다. 원래 버그는 일부 에이전트가 언어 지시를 조용히 누락해서
발생했는데(6b384f7에서 수정), 이 테스트는 그 불변식(invariant)을 명문화하여
미래의 리팩터링이 이를 슬그머니 다시 빠뜨리지 못하게 합니다.
"""
from pathlib import Path

import pytest

from tradingagents.agents.utils.agent_utils import get_language_instruction

_AGENTS_DIR = Path(__file__).resolve().parents[1] / "tradingagents" / "agents"

# 생성한 텍스트가 저장되는 보고서에 도달하는 모든 노드 목록. 보고서를 생성하는
# 에이전트를 새로 추가하면 여기에 등록하고 get_language_instruction()을 호출하게 하세요.
REPORT_AGENTS = [
    "analysts/market_analyst.py",
    "analysts/news_analyst.py",
    "analysts/fundamentals_analyst.py",
    "analysts/sentiment_analyst.py",
    "researchers/bull_researcher.py",
    "researchers/bear_researcher.py",
    "managers/research_manager.py",
    "managers/portfolio_manager.py",
    "risk_mgmt/aggressive_debator.py",
    "risk_mgmt/conservative_debator.py",
    "risk_mgmt/neutral_debator.py",
    "trader/trader.py",
]


@pytest.mark.unit
class TestLanguageInstruction:
    """언어 지시문 생성 함수의 동작을 검증하는 테스트 묶음."""

    def test_english_adds_no_tokens(self, monkeypatch):
        """출력 언어가 English면 불필요한 지시문을 추가하지 않는지 검증하는 테스트."""
        from tradingagents.dataflows.config import set_config
        set_config({"output_language": "English"})
        assert get_language_instruction() == ""

    def test_non_english_emits_directive(self):
        """영어가 아닌 언어 설정 시 해당 언어로 답하라는 지시문이 생성되는지 검증하는 테스트."""
        from tradingagents.dataflows.config import set_config
        set_config({"output_language": "中文"})
        out = get_language_instruction()
        assert "中文" in out
        assert "entire response" in out


@pytest.mark.unit
@pytest.mark.parametrize("rel", REPORT_AGENTS)
def test_report_agent_applies_language_instruction(rel):
    """각 보고서 생성 에이전트의 소스가 get_language_instruction()을 호출하는지 검증하는 테스트."""
    path = _AGENTS_DIR / rel
    assert path.exists(), f"missing agent module: {rel}"
    src = path.read_text(encoding="utf-8")
    assert "get_language_instruction()" in src, (
        f"{rel} does not apply get_language_instruction(); its output would "
        f"ignore the configured output_language (#740/#801)."
    )
