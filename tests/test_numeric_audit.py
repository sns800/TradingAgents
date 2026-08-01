"""[모듈 개요] 사후 수치 대조 감사(numeric audit, 설계분석 중기 로드맵 #5) 테스트.

PM 결정문에 인용된 달러 가격을 검증 스냅샷의 수치 집합과 결정론적으로
대조하는 tradingagents/graph/numeric_audit.py를 검증한다:

  1. 가격 추출 패턴 — $1,234.56 / 천 단위 콤마 / 무소수점 / 규모 표현 제외
  2. 스냅샷 수치 추출 — 마크다운 표의 맨 숫자
  3. 대조 로직 — 스냅샷 내 수치 통과, ±1% 허용 오차, 미발견 시 경고
  4. 경고 블록 형식 — 나열식(강한 주장 금지), 원문 보존, 중복 감사 방지
  5. 빈 스냅샷/가격 인용 없음 시 감사 생략
  6. 경고 블록이 등급 파싱(parse_rating)을 바꾸지 않음
"""
from __future__ import annotations

import pytest

from tradingagents.agents.utils.rating import parse_rating
from tradingagents.graph.numeric_audit import (
    AUDIT_WARNING_PREFIX,
    audit_final_decision,
    extract_cited_prices,
    extract_snapshot_numbers,
)

SNAPSHOT = (
    "## Verified market data snapshot for NVDA\n\n"
    "| Field | Value |\n|---|---:|\n"
    "| Open | 121.00 |\n| High | 125.50 |\n| Low | 119.75 |\n"
    "| Close | 123.45 |\n| Volume | 1000000 |\n\n"
    "| Indicator | Value |\n|---|---:|\n"
    "| close_50_sma | 1234.56 |\n| rsi | 55.10 |\n"
)


# ---------------------------------------------------------------------------
# 1. 달러 가격 추출
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPriceExtraction:
    def test_extracts_common_price_formats(self):
        """소수점/천 단위 콤마/무소수점 달러 가격이 모두 추출되는지 검증하는 테스트."""
        text = "Entry at $123.45, target $1,234.56, stop near $1,200, or $99."
        assert extract_cited_prices(text) == [
            ("$123.45", 123.45),
            ("$1,234.56", 1234.56),
            ("$1,200", 1200.0),
            ("$99", 99.0),
        ]

    def test_deduplicates_repeated_prices(self):
        """같은 가격이 여러 번 인용되면 한 번만 반환(등장 순서 유지)하는지 검증하는 테스트."""
        text = "Buy at $100.50; yes, $100.50 again, then $101."
        assert extract_cited_prices(text) == [("$100.50", 100.5), ("$101", 101.0)]

    def test_skips_scale_amounts(self):
        """"$5 billion" 같은 규모 표현(시총·매출)은 가격으로 취급하지 않는지 검증하는 테스트."""
        text = (
            "Market cap of $3 trillion, revenue of $5 billion, about $20bn "
            "cash, a $2M buyback and $10K retail flows."
        )
        assert extract_cited_prices(text) == []

    def test_plain_numbers_without_dollar_sign_are_ignored(self):
        """달러 기호 없는 맨 숫자(날짜·백분율 등)는 검사 대상이 아닌지 검증하는 테스트."""
        text = "RSI at 55.1 on 2026-01-01, up 3.2% over 5 days."
        assert extract_cited_prices(text) == []


# ---------------------------------------------------------------------------
# 2. 스냅샷 수치 추출
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSnapshotNumbers:
    def test_extracts_table_values(self):
        """스냅샷 마크다운 표의 수치들이 대조 기준 집합에 들어가는지 검증하는 테스트."""
        numbers = extract_snapshot_numbers(SNAPSHOT)
        for expected in (121.00, 125.50, 119.75, 123.45, 1000000.0, 1234.56, 55.10):
            assert expected in numbers

    def test_handles_comma_formatted_values(self):
        """콤마가 섞인 수치(1,234.56)도 정상 파싱되는지 검증하는 테스트."""
        assert 1234.56 in extract_snapshot_numbers("| Close | 1,234.56 |")


# ---------------------------------------------------------------------------
# 3. 대조 로직과 경고 블록
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAudit:
    def test_prices_present_in_snapshot_pass(self):
        """스냅샷에 존재하는 가격 인용은 경고 없이 통과하는지 검증하는 테스트."""
        decision = "**Rating**: Buy\n\nClose $123.45, resistance $125.50, sma $1,234.56."
        assert audit_final_decision(decision, SNAPSHOT) == decision

    def test_tolerance_allows_one_percent_deviation(self):
        """±1% 이내의 가격(반올림·근사 인용)은 통과하는지 검증하는 테스트."""
        decision = "**Rating**: Buy\n\nRoughly $124 near the close."  # 123.45의 +0.45%
        assert audit_final_decision(decision, SNAPSHOT) == decision

    def test_price_outside_tolerance_is_flagged(self):
        """허용 오차 밖의 가격 인용에 경고 블록이 붙는지 검증하는 테스트."""
        decision = "**Rating**: Buy\n\nAdd aggressively below $999.99."
        audited = audit_final_decision(decision, SNAPSHOT)
        assert audited.startswith(decision)  # 원문은 그대로 보존
        warning = audited[len(decision):]
        assert AUDIT_WARNING_PREFIX in warning
        assert "were not found in the verified market snapshot" in warning
        assert "$999.99" in warning

    def test_warning_lists_only_unmatched_prices(self):
        """일치한 가격은 나열하지 않고 미발견 가격만 경고에 담는지 검증하는 테스트."""
        decision = "**Rating**: Buy\n\nClose $123.45 with target $999.99 and stop $888."
        audited = audit_final_decision(decision, SNAPSHOT)
        warning = audited.split(AUDIT_WARNING_PREFIX, 1)[1]
        assert "$999.99" in warning
        assert "$888" in warning
        assert "$123.45" not in warning

    def test_warning_is_listing_style_not_accusatory(self):
        """경고가 나열식이며 계산된 값 가능성을 인정(강한 주장 금지)하는지 검증하는 테스트."""
        decision = "**Rating**: Sell\n\nStop-loss at $777.77."
        audited = audit_final_decision(decision, SNAPSHOT)
        assert "may be derived levels" in audited
        assert "hallucination" not in audited.lower()

    def test_custom_tolerance(self):
        """허용 오차 인자를 넓히면 경계 값이 통과하는지 검증하는 테스트."""
        decision = "Target $130."  # 최근접 스냅샷 값 125.50 대비 약 +3.6%
        assert AUDIT_WARNING_PREFIX in audit_final_decision(decision, SNAPSHOT)
        assert audit_final_decision(decision, SNAPSHOT, tolerance=0.05) == decision

    def test_audit_is_idempotent(self):
        """이미 감사된 결정문을 다시 감사해도 경고가 중복되지 않는지 검증하는 테스트."""
        decision = "**Rating**: Buy\n\nTarget $999.99."
        once = audit_final_decision(decision, SNAPSHOT)
        twice = audit_final_decision(once, SNAPSHOT)
        assert twice == once
        assert twice.count(AUDIT_WARNING_PREFIX) == 1


# ---------------------------------------------------------------------------
# 4. 감사 생략 경로
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAuditSkips:
    def test_empty_snapshot_skips_audit(self):
        """스냅샷이 비어 있으면(NO_DATA 등) 결정문을 그대로 반환하는지 검증하는 테스트."""
        decision = "**Rating**: Buy\n\nTarget $999.99."
        assert audit_final_decision(decision, "") == decision
        assert audit_final_decision(decision, "   ") == decision
        assert audit_final_decision(decision, None) == decision

    def test_empty_decision_passes_through(self):
        """빈 결정문은 그대로 반환되는지 검증하는 테스트."""
        assert audit_final_decision("", SNAPSHOT) == ""

    def test_decision_without_dollar_prices_untouched(self):
        """달러 가격 인용이 없는 결정문에는 어떤 경고도 붙지 않는지 검증하는 테스트."""
        decision = "**Rating**: Hold\n\nMomentum is fading; wait for confirmation."
        assert audit_final_decision(decision, SNAPSHOT) == decision


# ---------------------------------------------------------------------------
# 5. 등급 파싱 안전성 (경고 블록이 결정을 바꾸지 않음)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRatingSafety:
    @pytest.mark.parametrize(
        "rating", ["Buy", "Overweight", "Hold", "Underweight", "Sell"]
    )
    def test_warning_block_does_not_change_parsed_rating(self, rating):
        """경고 블록이 붙어도 parse_rating 결과가 그대로인지 5개 등급 전부 검증하는 테스트."""
        decision = f"**Rating**: {rating}\n\nKey level at $999.99."
        audited = audit_final_decision(decision, SNAPSHOT)
        assert AUDIT_WARNING_PREFIX in audited
        assert parse_rating(audited) == rating

    def test_label_free_decision_still_parses_first_rating_word(self):
        """등급 라벨 없이 본문 어휘로 파싱되는 결정문도 경고 추가 후 결과가 같은지 검증."""
        decision = "I recommend Underweight given the setup near $999.99."
        audited = audit_final_decision(decision, SNAPSHOT)
        assert parse_rating(audited) == parse_rating(decision) == "Underweight"

    def test_forced_hold_decision_is_never_audited(self):
        """강제 Hold(NO_DATA 게이트) 경로에서는 스냅샷이 비어 감사가 생략되는지 검증.

        market_data_ok=False가 되는 실행에서는 verified_snapshot도 빈
        문자열로 보존되므로(센티널 -> 빈 값), 강제 Hold 결정문은 항상
        감사 생략 경로를 탄다.
        """
        from tradingagents.agents.managers.portfolio_manager import FORCED_HOLD_DECISION

        assert audit_final_decision(FORCED_HOLD_DECISION, "") == FORCED_HOLD_DECISION
        assert parse_rating(FORCED_HOLD_DECISION) == "Hold"
