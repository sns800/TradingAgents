"""심판(RM·PM) 토론 이력 예산 제한 테스트 (depth=5 'Input is too long' 회귀 방지).

깊은 토론(2N+1 / 3N+1 턴)에서 심판 프롬프트의 이력이 폭증해 모델 입력
한도를 넘기던 회귀를 condense_for_judge로 막는다. 예산보다 짧으면 원문을
그대로 통과시켜 depth 1~3의 기존 동작을 보존하는 것이 핵심 불변식이다.
"""
from __future__ import annotations

import pytest

from tradingagents.agents.utils.debate_context import (
    DEFAULT_JUDGE_BUDGET_CHARS,
    TRUNCATION_MARKER,
    condense_for_judge,
)


def _statement(label: str, n: int, size: int) -> str:
    return f"{label}: round {n} " + ("가" * size)


@pytest.mark.unit
class TestCondenseForJudge:
    def test_short_history_passes_through_unchanged(self):
        """예산 이하 이력(depth 1~3 대표)은 원문 그대로 — 기존 동작 보존."""
        hist = "\n".join(
            _statement(lbl, i, 500)
            for i, lbl in enumerate(["Bull Analyst", "Bear Analyst", "Bull Analyst"])
        )
        assert len(hist) < DEFAULT_JUDGE_BUDGET_CHARS
        assert condense_for_judge(hist) == hist

    def test_large_history_is_bounded(self):
        """예산 초과 이력(depth 5 대표)은 총 길이가 크게 줄어든다."""
        # 11턴 × 각 ~6000자 ≈ 66k자 (예산 40k 초과)
        labels = ["Bull Analyst", "Bear Analyst"] * 6
        hist = "\n".join(_statement(labels[i], i, 6000) for i in range(11))
        assert len(hist) > DEFAULT_JUDGE_BUDGET_CHARS
        out = condense_for_judge(hist)
        assert len(out) < len(hist)
        # 최신 발언 전문 + 과거 절단 → 예산 + 여유분 이내로 제한
        assert len(out) <= DEFAULT_JUDGE_BUDGET_CHARS + 11 * 1400
        assert TRUNCATION_MARKER in out

    def test_most_recent_statement_kept_full(self):
        """가장 최근 발언은 전문 유지 — 판정의 핵심 근거."""
        labels = ["Bull Analyst", "Bear Analyst"] * 6
        last = _statement("Bear Analyst", 999, 3000)
        hist = "\n".join(_statement(labels[i], i, 6000) for i in range(10)) + "\n" + last
        out = condense_for_judge(hist)
        assert last in out

    def test_empty_and_unlabeled(self):
        assert condense_for_judge("") == ""
        # 라벨 없는 형식: 예산 초과 시 뒤에서 잘라 안전 반환, 이하면 원문
        blob = "가" * (DEFAULT_JUDGE_BUDGET_CHARS + 5000)
        out = condense_for_judge(blob)
        assert len(out) <= DEFAULT_JUDGE_BUDGET_CHARS

    def test_rm_and_pm_use_budget(self):
        """RM·PM 노드가 condense_for_judge를 실제로 호출하는지 소스 검사."""
        from pathlib import Path

        root = Path(__file__).resolve().parents[1] / "tradingagents" / "agents" / "managers"
        for fn in ("research_manager.py", "portfolio_manager.py"):
            src = (root / fn).read_text(encoding="utf-8")
            assert "condense_for_judge(" in src, f"{fn}가 예산 제한을 쓰지 않음"
