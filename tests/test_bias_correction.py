# =============================================================================
# [테스트 개요]
# 강세 편향 교정(편향검증 Phase 2)의 불변식을 명문화하는 테스트.
#
# 근거: ~/.tradingagents/logs/bias_probe/run_main/summary.md — 분리 실험에서
#   (1) "결단하라/Hold를 아껴라" 문구가 편향 주범 (control 44% 강세),
#   (2) 단순 제거는 반대 극단으로 과교정 (no-anti-hold 89% Hold),
#   (3) 루브릭 점수를 등급보다 먼저 출력하면 증거 비례 판정에 근접,
#   (4) RM 등급이 최종 등급과 27/27 일치 → RM이 최우선 교정 지점.
#
# 검증하는 불변식:
#   - RM/스키마에 반(反)Hold 문구가 없고 기저율 균형 문구가 있다 (소스 검사)
#   - ResearchPlan의 루브릭 점수 필드 6종이 recommendation보다 앞 순서다
#   - render_research_plan이 루브릭 점수 표를 포함한다
#   - PM 등급 척도가 Buy/Sell 대칭이고 "Be decisive"가 없다 (소스 검사)
#   - 공격 토론자가 "트레이더 결정 옹호" 에코 루프 문구를 갖지 않는다
#   - 감성 밴드 설명이 Neutral을 과도하게 제한하지 않는다
# =============================================================================
"""편향검증 Phase 2 교정 불변식 테스트 (소스 검사 + 스키마 순서 + 렌더러)."""

from pathlib import Path

import pytest

from tradingagents.agents.schemas import (
    PortfolioRating,
    ResearchPlan,
    SentimentReport,
    render_research_plan,
)

_ROOT = Path(__file__).resolve().parents[1]
_AGENTS = _ROOT / "tradingagents" / "agents"


def _src(rel: str) -> str:
    return (_AGENTS / rel).read_text(encoding="utf-8")


# 루브릭 점수 필드 6종 — 스키마 선언 순서대로 (편향검증 Phase 2).
SCORE_FIELDS = (
    "bull_evidence_score",
    "bear_evidence_score",
    "bull_responsiveness_score",
    "bear_responsiveness_score",
    "bull_risk_asymmetry_score",
    "bear_risk_asymmetry_score",
)


def _make_plan(**scores) -> ResearchPlan:
    kwargs = dict.fromkeys(SCORE_FIELDS, 0)
    kwargs.update(scores)
    return ResearchPlan(
        recommendation=PortfolioRating.HOLD,
        bull_case_assessment="ba",
        bear_case_assessment="be",
        rationale="r",
        strategic_actions="s",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 1. 리서치 매니저: 반Hold 문구 부재 + 기저율 균형 문구 존재
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestResearchManagerWording:
    def test_anti_hold_phrases_removed(self):
        """편향 주범으로 확인된 결단 강요/Hold 회피 문구가 RM 소스에 없는지 검증."""
        src = _src("managers/research_manager.py")
        assert "Commit to a clear stance" not in src
        assert "reserve Hold" not in src

    def test_base_rate_wording_present(self):
        """기저율 균형(증거 비례 판정) 문구가 RM 프롬프트 소스에 있는지 검증."""
        src = _src("managers/research_manager.py")
        assert "Rate in proportion to the evidence" in src
        assert "roughly equally common" in src
        assert "Hold is a legitimate finding" in src

    def test_score_before_rating_instruction_present(self):
        """점수를 등급보다 먼저 매기라는 소프트 결합 지시가 있는지 검증.

        결정론적 강제 변환(score→rating)은 프로브에서 전원 Hold 과교정이
        확인되어 도입하지 않는다 — 지시는 소프트 결합이어야 한다.
        """
        src = _src("managers/research_manager.py")
        assert "Score the rubric before you rate" in src
        assert "consistent with those scores" in src

    def test_rubric_criteria_preserved(self):
        """기존 루브릭 3항목이 교정 후에도 보존되는지 검증."""
        src = _src("managers/research_manager.py")
        for marker in ("Evidence grounding", "Responsiveness", "Risk asymmetry"):
            assert marker in src, f"rubric criterion {marker!r} missing"


# ---------------------------------------------------------------------------
# 2. ResearchPlan 스키마: 반Hold 설명 제거 + 점수 필드가 recommendation보다 앞
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestResearchPlanSchema:
    def test_recommendation_description_neutralized(self):
        """recommendation 설명에서 '더 강한 쪽에 손 들라' 문구가 제거됐는지 검증."""
        desc = ResearchPlan.model_fields["recommendation"].description
        assert "commit to the side with the stronger arguments" not in desc
        assert "Reserve Hold" not in desc
        # 대칭·기저율 취지의 중립 문구로 교체됐다.
        assert "Rate in proportion to the evidence" in desc
        assert "Hold is a legitimate finding" in desc

    def test_score_fields_come_before_recommendation(self):
        """루브릭 점수 필드 6종이 recommendation보다 앞 순서인지 검증.

        구조화 출력에서 필드 선언 순서가 곧 생성 순서이므로, 점수가 등급보다
        먼저 나와야 점수 선출력(score-first) 효과가 실린다.
        """
        order = list(ResearchPlan.model_fields)
        rec_idx = order.index("recommendation")
        for field in SCORE_FIELDS:
            assert order.index(field) < rec_idx, (
                f"{field} must be declared before recommendation"
            )

    def test_score_fields_are_bounded_ints(self):
        """점수 필드가 -5~+5 정수로 검증되는지 확인 (범위 밖은 거부)."""
        from pydantic import ValidationError

        plan = _make_plan(bull_evidence_score=5, bear_evidence_score=-5)
        assert plan.bull_evidence_score == 5
        with pytest.raises(ValidationError):
            _make_plan(bull_evidence_score=6)
        with pytest.raises(ValidationError):
            _make_plan(bear_risk_asymmetry_score=-6)

    def test_score_fields_are_required(self):
        """점수 필드가 선택이 아니라 필수인지 검증 — LLM이 채우도록 스키마가 강제."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ResearchPlan(
                recommendation=PortfolioRating.HOLD,
                bull_case_assessment="ba",
                bear_case_assessment="be",
                rationale="r",
                strategic_actions="s",
            )


# ---------------------------------------------------------------------------
# 3. 렌더러: 루브릭 점수 표 포함
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRendererScoreTable:
    def test_render_includes_score_table(self):
        """render_research_plan 출력에 루브릭 점수 표가 포함되는지 검증."""
        md = render_research_plan(_make_plan(
            bull_evidence_score=3, bear_evidence_score=-2,
            bull_responsiveness_score=1, bear_responsiveness_score=4,
            bull_risk_asymmetry_score=-1, bear_risk_asymmetry_score=2,
        ))
        assert "**Rubric Scores**" in md
        assert "| Criterion | Bull | Bear |" in md
        assert "| Evidence grounding | +3 | -2 |" in md
        assert "| Responsiveness | +1 | +4 |" in md
        assert "| Risk asymmetry | -1 | +2 |" in md
        # 기존 섹션 헤더는 그대로 보존된다 (하위 호환).
        for header in (
            "**Recommendation**", "**Bull Case Assessment**",
            "**Bear Case Assessment**", "**Rationale**", "**Strategic Actions**",
        ):
            assert header in md


# ---------------------------------------------------------------------------
# 4. 포트폴리오 매니저: 척도 대칭성 + Be decisive 제거
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPortfolioManagerSymmetry:
    def test_be_decisive_removed_and_proportional_wording_present(self):
        """'Be decisive' 지시가 증거 비례 문구로 교체됐는지 검증 (소스 검사)."""
        src = _src("managers/portfolio_manager.py")
        assert "Be decisive" not in src
        assert "Rate in proportion to the evidence" in src
        assert "roughly equally common" in src

    def test_rating_scale_is_symmetric(self):
        """등급 척도의 Buy/Sell, Overweight/Underweight 대칭성 검증 (소스 검사).

        기존 비대칭: 'strong conviction'이 Buy에만 있었고, Underweight는
        'take partial profits'(이익을 전제하는 프레임)였다.
        """
        src = _src("managers/portfolio_manager.py")
        assert "take partial profits" not in src
        # Buy와 Sell 모두 strong conviction 프레임을 가진다.
        assert "**Buy**: Strong conviction" in src
        assert "**Sell**: Strong conviction" in src
        # Overweight/Underweight 모두 점진(gradually) 프레임을 가진다.
        assert "**Overweight**: Favorable outlook, gradually increase exposure" in src
        assert "**Underweight**: Cautious outlook, gradually reduce exposure" in src


# ---------------------------------------------------------------------------
# 5. 공격 토론자: 트레이더 결정 에코 루프 해소
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAggressiveDebatorEchoLoop:
    def test_no_unconditional_advocacy_of_trader_decision(self):
        """'트레이더 결정을 옹호하라' 고정 문구가 없는지 검증 (소스 검사)."""
        src = _src("risk_mgmt/aggressive_debator.py")
        assert "compelling case for the trader's decision" not in src

    def test_role_is_evidence_reading_not_decision(self):
        """결정 방향과 무관하게 '증거의 공격적 해석'을 옹호하는 역할인지 검증."""
        src = _src("risk_mgmt/aggressive_debator.py")
        assert "not the trader's decision itself" in src
        # 계획이 과소/과대 위험일 때 각각 그 방향으로 비판하라는 지시.
        assert "less risk than the evidence justifies" in src
        assert "overreaches beyond what the evidence supports" in src


# ---------------------------------------------------------------------------
# 6. 감성 밴드: Neutral 과도 제한 완화
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSentimentNeutralAllowed:
    def test_schema_band_description_softened(self):
        """overall_band 설명의 'Use Neutral only when...' 제한이 완화됐는지 검증."""
        desc = SentimentReport.model_fields["overall_band"].description
        assert "Use Neutral only when" not in desc
        assert "Neutral is a legitimate call" in desc

    def test_sentiment_prompt_fallback_matches_schema(self):
        """감성 분석가 프롬프트 본문(자유 텍스트 지시)도 동일하게 완화됐는지 검증."""
        src = _src("analysts/sentiment_analyst.py")
        assert "Neutral only when all sources are genuinely silent" not in src
        assert "Neutral is a legitimate call" in src
