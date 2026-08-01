# 이 파일은 평가 하네스의 스코어보드 집계 로직을 검증하는 테스트 모음입니다.
# 가짜 메모리 로그 항목으로 등급별 방향 적중률·평균 알파 계산, Hold 임계값,
# 항상-Hold/랜덤 베이스라인 기대값, 마크다운 렌더링을 결정론적으로 확인합니다.
"""스코어보드 집계 테스트 — 적중률/알파 계산, Hold 임계값, 베이스라인 비교."""

import pytest

from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.eval.scoreboard import (
    aggregate_entries,
    is_directional_hit,
    parse_percent,
    render_markdown,
)

# ---------------------------------------------------------------------------
# 공용 헬퍼
# ---------------------------------------------------------------------------


def _entry(rating, alpha, raw=None, pending=False):
    """스코어보드 입력용 가짜 메모리 로그 항목을 만든다."""
    return {
        "rating": rating,
        "alpha": alpha,
        "raw": raw if raw is not None else alpha,
        "pending": pending,
    }


# 표본: 총 6건, 임계값 0.01 기준 적중 4건
SAMPLE_ENTRIES = [
    _entry("Buy", 0.05),          # 적중 (알파 > 0)
    _entry("Buy", -0.02),         # 미적중
    _entry("Sell", -0.03),        # 적중 (알파 < 0)
    _entry("Hold", 0.005),        # 적중 (|알파| < 0.01)
    _entry("Overweight", 0.002),  # 적중 (알파 > 0)
    _entry("Underweight", 0.01),  # 미적중 (알파 > 0)
]


# ---------------------------------------------------------------------------
# parse_percent
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "value,expected",
    [
        ("+1.2%", 0.012),
        ("-0.5%", -0.005),
        ("+0.0%", 0.0),
        (0.03, 0.03),
        (-1, -1.0),
        ("0.02", 0.02),  # 퍼센트 기호 없는 문자열은 소수 비율로 간주
    ],
)
def test_parse_percent_valid(value, expected):
    assert parse_percent(value) == pytest.approx(expected)


@pytest.mark.unit
@pytest.mark.parametrize("value", [None, "", "n/a", "pending", "garbage", True])
def test_parse_percent_invalid_returns_none(value):
    # 불리언 True는 숫자처럼 보이지만 수익률이 아니므로 거부되어야 한다
    if value is True:
        assert parse_percent(value) is None
    else:
        assert parse_percent(value) is None


# ---------------------------------------------------------------------------
# is_directional_hit — 방향 적중 규칙
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "rating,alpha,expected",
    [
        ("Buy", 0.01, True),
        ("Buy", -0.01, False),
        ("Buy", 0.0, False),           # 알파 0은 강세 예측의 적중이 아님
        ("Overweight", 0.001, True),
        ("Sell", -0.02, True),
        ("Sell", 0.02, False),
        ("Underweight", -0.001, True),
        ("Hold", 0.005, True),          # |알파| < 0.01
        ("Hold", -0.005, True),
        ("Hold", 0.02, False),
        ("Hold", 0.01, False),          # 경계값은 미적중 (엄격 부등호)
    ],
)
def test_directional_hit_rules(rating, alpha, expected):
    assert is_directional_hit(rating, alpha, hold_threshold=0.01) is expected


@pytest.mark.unit
def test_hold_threshold_is_configurable():
    # 같은 항목이 임계값에 따라 적중/미적중으로 바뀌어야 한다
    assert is_directional_hit("Hold", 0.015, hold_threshold=0.01) is False
    assert is_directional_hit("Hold", 0.015, hold_threshold=0.02) is True


# ---------------------------------------------------------------------------
# aggregate_entries — 등급별 집계
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_aggregate_per_rating_counts_and_hit_rates():
    summary = aggregate_entries(SAMPLE_ENTRIES, hold_threshold=0.01)

    per = summary["per_rating"]
    assert per["Buy"]["count"] == 2
    assert per["Buy"]["hits"] == 1
    assert per["Buy"]["hit_rate"] == pytest.approx(0.5)
    assert per["Buy"]["avg_alpha"] == pytest.approx(0.015)  # (0.05 - 0.02) / 2
    assert per["Sell"]["count"] == 1
    assert per["Sell"]["hit_rate"] == pytest.approx(1.0)
    assert per["Hold"]["hit_rate"] == pytest.approx(1.0)
    assert per["Underweight"]["hit_rate"] == pytest.approx(0.0)

    overall = summary["overall"]
    assert overall["count"] == 6
    assert overall["hits"] == 4
    assert overall["hit_rate"] == pytest.approx(4 / 6)


@pytest.mark.unit
def test_aggregate_average_raw_and_alpha():
    entries = [
        _entry("Buy", 0.02, raw="+3.0%"),
        _entry("Buy", "-1.0%", raw="+1.0%"),
    ]
    summary = aggregate_entries(entries, hold_threshold=0.01)
    buy = summary["per_rating"]["Buy"]
    assert buy["avg_raw"] == pytest.approx(0.02)            # (0.03 + 0.01) / 2
    assert buy["avg_alpha"] == pytest.approx(0.005)          # (0.02 - 0.01) / 2


@pytest.mark.unit
def test_aggregate_hold_threshold_changes_hold_hits():
    entries = [_entry("Hold", 0.015)]
    strict = aggregate_entries(entries, hold_threshold=0.01)
    loose = aggregate_entries(entries, hold_threshold=0.02)
    assert strict["per_rating"]["Hold"]["hits"] == 0
    assert loose["per_rating"]["Hold"]["hits"] == 1


@pytest.mark.unit
def test_aggregate_skips_pending_and_unparsable():
    entries = SAMPLE_ENTRIES + [
        _entry("Buy", 0.05, pending=True),   # pending → 제외
        _entry("Buy", "n/a"),                # 알파 파싱 불가 → 제외
    ]
    summary = aggregate_entries(entries, hold_threshold=0.01)
    assert summary["total"] == 6
    assert summary["skipped"] == 2


@pytest.mark.unit
def test_aggregate_empty_input():
    summary = aggregate_entries([], hold_threshold=0.01)
    assert summary["total"] == 0
    assert summary["overall"]["hit_rate"] is None
    assert summary["baselines"]["always_hold_hit_rate"] is None
    assert summary["baselines"]["random_hit_rate"] is None


# ---------------------------------------------------------------------------
# 베이스라인 비교 — 같은 표본에서의 기대값
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_always_hold_baseline_expected_value():
    # |알파| < 0.01인 항목은 6건 중 2건(0.005, 0.002)
    summary = aggregate_entries(SAMPLE_ENTRIES, hold_threshold=0.01)
    assert summary["baselines"]["always_hold_hit_rate"] == pytest.approx(2 / 6)


@pytest.mark.unit
def test_random_baseline_expected_value():
    # 항목별 기대 적중 확률 = (2*[알파>0] + 2*[알파<0] + [|알파|<임계값]) / 5
    #   0.05→0.4, -0.02→0.4, -0.03→0.4, 0.005→0.6, 0.002→0.6, 0.01→0.4
    summary = aggregate_entries(SAMPLE_ENTRIES, hold_threshold=0.01)
    expected = (0.4 * 4 + 0.6 * 2) / 6
    assert summary["baselines"]["random_hit_rate"] == pytest.approx(expected)


# ---------------------------------------------------------------------------
# 마크다운 렌더링
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_render_markdown_contains_tables_and_values():
    summary = aggregate_entries(SAMPLE_ENTRIES, hold_threshold=0.01)
    markdown = render_markdown(summary)
    assert "| 등급 | 건수 |" in markdown
    assert "| Buy | 2 | 50.0% |" in markdown
    assert "항상-Hold" in markdown
    assert "랜덤" in markdown
    # 전체 적중률 4/6 = 66.7%
    assert "66.7%" in markdown


@pytest.mark.unit
def test_render_markdown_empty_sample():
    markdown = render_markdown(aggregate_entries([], hold_threshold=0.01))
    assert "n/a" in markdown


# ---------------------------------------------------------------------------
# 실제 메모리 로그 파일과의 통합 — load_entries() 출력 형식 호환
# ---------------------------------------------------------------------------

_SEP = TradingMemoryLog._SEPARATOR


@pytest.mark.unit
def test_aggregate_from_real_memory_log_file(tmp_path):
    log_path = tmp_path / "trading_memory.md"
    log_path.write_text(
        "[2025-01-06 | AAPL | Buy | +2.0% | +1.0% | 5d]\n\n"
        "DECISION:\nRating: Buy\nGo long.\n\n"
        "REFLECTION:\nDirection was right."
        + _SEP
        + "[2025-01-13 | AAPL | Sell | -1.0% | +0.5% | 5d]\n\n"
        "DECISION:\nRating: Sell\nExit.\n\n"
        "REFLECTION:\nDirection was wrong."
        + _SEP
        + "[2025-01-20 | AAPL | Hold | pending]\n\n"
        "DECISION:\nRating: Hold\nWait."
        + _SEP,
        encoding="utf-8",
    )
    log = TradingMemoryLog({"memory_log_path": str(log_path)})
    summary = aggregate_entries(log.load_entries(), hold_threshold=0.01)

    assert summary["total"] == 2          # pending 1건은 제외
    assert summary["skipped"] == 1
    assert summary["per_rating"]["Buy"]["hits"] == 1        # 알파 +1.0% > 0
    assert summary["per_rating"]["Sell"]["hits"] == 0       # 알파 +0.5% > 0 → 미적중
    assert summary["per_rating"]["Buy"]["avg_raw"] == pytest.approx(0.02)
    assert summary["per_rating"]["Sell"]["avg_alpha"] == pytest.approx(0.005)
