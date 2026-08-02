# =============================================================================
# [테스트 개요]
# 리스크 토론 대칭화(편향검증 b')의 불변식을 명문화하는 소스 검사 테스트.
#
# 근거: PM을 리스크 감독 게이트로 강화했더니 override 22/40이 전부 하향(0 상향),
#   문턱 2회 상향에도 55% 정체. 근본 원인은 PM이 아니라 리스크 토론 자체의 하방
#   편향 — 보수 분석가가 하방을 과생산하고, 공격 반박이 일반론에 그치며, 중립이
#   기본값으로 "신중" 쪽에 기울었다. 연구 강세 편향의 거울상.
#
# 검증하는 불변식:
#   - 보수 토론자: 하방이 decision-relevant 하려면 구체적·정량·미반영이어야 한다는
#     규율이 있고, 자산 보호 최우선/저위험 전략 옹호式 과생산 프레이밍은 제거됐다.
#   - 공격 토론자: 보수 논거에 구체적 반증(일반론 금지)으로 반박하고, 상방도 정량
#     근거를 요구하며, Phase 2의 증거 해석 옹호 역할은 보존된다.
#   - 중립 토론자: 기본값이 "신중"이 아니라 증거 방향이며, 기저율 균형 문구가 있고,
#     "moderate/sustainable/most reliable outcomes"式 신중 기울기 문구는 제거됐다.
#   - 3인 공통 보존: get_language_instruction, condense_debate_history, 4종 보고서 주입.
# =============================================================================
"""리스크 토론 대칭화(편향검증 b') 불변식 테스트 (소스 검사)."""

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_RISK = _ROOT / "tradingagents" / "agents" / "risk_mgmt"


def _src(name: str) -> str:
    return (_RISK / name).read_text(encoding="utf-8")


@pytest.mark.unit
class TestConservativeSymmetrized:
    def test_decision_relevant_discipline_present(self):
        """하방이 decision-relevant 하려면 구체적·정량·미반영이어야 한다는 규율."""
        src = _src("conservative_debator.py")
        assert "decision-relevant" in src
        assert "concrete, material, quantitatively grounded" in src
        assert "not already reflected" in src

    def test_no_downside_overproduction_framing(self):
        """하방 과생산을 유발하던 기존 프레이밍이 제거됐는지 검증."""
        src = _src("conservative_debator.py")
        assert "primary objective is to protect assets" not in src
        assert "convincing case for a low-risk approach" not in src
        assert "safest path for the firm's assets" not in src

    def test_do_not_pad_downside(self):
        """일반적 신중론 나열을 금지하는 명시 문구."""
        src = _src("conservative_debator.py")
        assert "Do not manufacture or pad the downside" in src


@pytest.mark.unit
class TestAggressiveSpecificRebuttal:
    def test_specific_counter_evidence_required(self):
        """보수/중립 논거에 구체적 반증(일반론 금지)으로 반박하라는 지시."""
        src = _src("aggressive_debator.py")
        assert "specific counter-evidence" in src
        assert "a general appeal to opportunity does not rebut a specific downside" in src

    def test_upside_requires_quantitative_grounding(self):
        """상방 논거도 하방과 동일한 정량 근거를 요구."""
        src = _src("aggressive_debator.py")
        assert "Ground your upside case in quantitative evidence" in src
        assert "same standard of proof you demand of the downside" in src

    def test_phase2_evidence_reading_role_preserved(self):
        """Phase 2의 '증거의 공격적 해석 옹호(결정 자체가 아님)' 역할이 보존된다."""
        src = _src("aggressive_debator.py")
        assert "not the trader's decision itself" in src
        assert "less risk than the evidence justifies" in src


@pytest.mark.unit
class TestNeutralTrulyBalanced:
    def test_default_is_evidence_not_caution(self):
        """기본값이 '신중'이 아니라 증거가 가리키는 쪽."""
        src = _src("neutral_debator.py")
        assert "Your default is not caution" in src
        assert "wherever the evidence points" in src

    def test_base_rate_balance_wording_present(self):
        """기저율 균형 문구(대형주 종목-일 상방·하방 알파 반반)가 있다."""
        src = _src("neutral_debator.py")
        assert "upside and downside alpha are roughly equally common" in src

    def test_caution_tilt_wording_removed(self):
        """'moderate/sustainable/most reliable outcomes'式 신중 기울기 제거."""
        src = _src("neutral_debator.py")
        assert "most reliable outcomes" not in src
        assert "moderate, sustainable strategy" not in src
        assert "safeguarding against extreme volatility" not in src


@pytest.mark.unit
class TestPreservedScaffolding:
    """3인 모두 언어 지시·이력 압축·보고서 주입 구조를 보존한다."""

    @pytest.mark.parametrize(
        "name",
        ["aggressive_debator.py", "conservative_debator.py", "neutral_debator.py"],
    )
    def test_scaffolding_preserved(self, name):
        src = _src(name)
        assert "get_language_instruction()" in src
        assert "condense_debate_history(history)" in src
        assert "Market Research Report:" in src
        assert 'risk_debate_state["count"] + 1' in src
