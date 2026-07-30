# 이 파일은 체크포인트(checkpoint) 저장·재개 기능을 검증하는 테스트 모음입니다.
# 분석 도중 프로그램이 중단(crash)되어도, 다시 실행하면 마지막으로 완료된
# 노드부터 이어서 진행되는지 확인합니다.
"""체크포인트 재개(resume) 테스트: 분석 도중 중단 후 재실행 시 마지막 노드부터 이어서 실행."""

import tempfile
import unittest
from typing import TypedDict

from langgraph.graph import END, StateGraph

from tradingagents.graph.checkpointer import (
    checkpoint_step,
    clear_checkpoint,
    get_checkpointer,
    has_checkpoint,
    thread_id,
)

# 첫 실행에서 중단(crash)을 흉내 내기 위한 가변 플래그
_should_crash = False


class _SimpleState(TypedDict):
    count: int


def _node_a(state: _SimpleState) -> dict:
    return {"count": state["count"] + 1}


def _node_b(state: _SimpleState) -> dict:
    if _should_crash:
        raise RuntimeError("simulated mid-analysis crash")
    return {"count": state["count"] + 10}


def _build_graph() -> StateGraph:
    builder = StateGraph(_SimpleState)
    builder.add_node("analyst", _node_a)
    builder.add_node("trader", _node_b)
    builder.set_entry_point("analyst")
    builder.add_edge("analyst", "trader")
    builder.add_edge("trader", END)
    return builder


class TestCheckpointResume(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.ticker = "TEST"
        self.date = "2026-04-20"

    def test_crash_and_resume(self):
        """'trader' 노드에서 중단된 뒤 체크포인트에서 이어서 실행되는지 검증하는 테스트."""
        global _should_crash
        builder = _build_graph()
        tid = thread_id(self.ticker, self.date)
        cfg = {"configurable": {"thread_id": tid}}

        # 1차 실행: trader 노드에서 중단 발생
        _should_crash = True
        with get_checkpointer(self.tmpdir, self.ticker) as saver:
            graph = builder.compile(checkpointer=saver)
            with self.assertRaises(RuntimeError):
                graph.invoke({"count": 0}, config=cfg)

        # 스텝 1(analyst 완료 시점)의 체크포인트가 존재해야 함
        self.assertTrue(has_checkpoint(self.tmpdir, self.ticker, self.date))
        step = checkpoint_step(self.tmpdir, self.ticker, self.date)
        self.assertEqual(step, 1)

        # 2차 실행: 재개 — 이번에는 trader가 성공
        _should_crash = False
        with get_checkpointer(self.tmpdir, self.ticker) as saver:
            graph = builder.compile(checkpointer=saver)
            result = graph.invoke(None, config=cfg)

        # analyst가 1을 더하고 trader가 10을 더함 → 11
        self.assertEqual(result["count"], 11)

    def test_clear_checkpoint_allows_fresh_start(self):
        """체크포인트를 삭제하면 그래프가 처음부터 새로 시작하는지 검증하는 테스트."""
        global _should_crash
        builder = _build_graph()
        tid = thread_id(self.ticker, self.date)
        cfg = {"configurable": {"thread_id": tid}}

        # 중단을 일으켜 체크포인트를 생성
        _should_crash = True
        with get_checkpointer(self.tmpdir, self.ticker) as saver:
            graph = builder.compile(checkpointer=saver)
            with self.assertRaises(RuntimeError):
                graph.invoke({"count": 0}, config=cfg)

        self.assertTrue(has_checkpoint(self.tmpdir, self.ticker, self.date))

        # 체크포인트 삭제
        clear_checkpoint(self.tmpdir, self.ticker, self.date)
        self.assertFalse(has_checkpoint(self.tmpdir, self.ticker, self.date))

        # 처음부터 새로 실행하면 성공함
        _should_crash = False
        with get_checkpointer(self.tmpdir, self.ticker) as saver:
            graph = builder.compile(checkpointer=saver)
            result = graph.invoke({"count": 0}, config=cfg)

        self.assertEqual(result["count"], 11)


    def test_different_date_starts_fresh(self):
        """다른 날짜로 실행하면 기존 체크포인트에서 재개하지 않고 새로 시작하는지 검증하는 테스트."""
        global _should_crash
        builder = _build_graph()
        date2 = "2026-04-21"

        # date1로 실행 — 중단을 일으켜 체크포인트를 남김
        _should_crash = True
        tid1 = thread_id(self.ticker, self.date)
        with get_checkpointer(self.tmpdir, self.ticker) as saver:
            graph = builder.compile(checkpointer=saver)
            with self.assertRaises(RuntimeError):
                graph.invoke({"count": 0}, config={"configurable": {"thread_id": tid1}})

        self.assertTrue(has_checkpoint(self.tmpdir, self.ticker, self.date))

        # date2에는 체크포인트가 없어야 함
        self.assertFalse(has_checkpoint(self.tmpdir, self.ticker, date2))

        # date2로 실행 — 처음부터 시작해 성공해야 함
        _should_crash = False
        tid2 = thread_id(self.ticker, date2)
        self.assertNotEqual(tid1, tid2)

        with get_checkpointer(self.tmpdir, self.ticker) as saver:
            graph = builder.compile(checkpointer=saver)
            result = graph.invoke({"count": 0}, config={"configurable": {"thread_id": tid2}})

        # 새 실행: analyst +1, trader +10 = 11
        self.assertEqual(result["count"], 11)

        # 원래 날짜의 체크포인트는 손대지 않은 채 그대로 존재함
        self.assertTrue(has_checkpoint(self.tmpdir, self.ticker, self.date))


class TestCheckpointSignature(unittest.TestCase):
    """그래프 형태(애널리스트 선택 / 토론 깊이 / 자산 모드)가 달라지면 이전 실행의
    체크포인트에서 재개하면 안 됨을 검증하는 테스트 묶음 (#1089)."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.ticker = "TEST"
        self.date = "2026-04-20"

    def test_empty_signature_is_legacy_id(self):
        """빈 시그니처(signature)는 기존(legacy) 스레드 ID와 동일함을 검증하는 테스트."""
        self.assertEqual(
            thread_id(self.ticker, self.date),
            thread_id(self.ticker, self.date, ""),
        )

    def test_signature_changes_thread_id(self):
        """시그니처가 다르면 스레드 ID도 달라짐을 검증하는 테스트."""
        legacy = thread_id(self.ticker, self.date)
        sig_a = thread_id(self.ticker, self.date, "analysts=market,news|asset=stock")
        sig_b = thread_id(self.ticker, self.date, "analysts=market|asset=stock")
        self.assertNotEqual(sig_a, sig_b)          # 그래프 형태가 다르면 ID도 다름
        self.assertNotEqual(legacy, sig_a)         # 시그니처 기반 ID는 기존 ID와 다름
        self.assertEqual(                          # 동일한 입력이면 항상 같은 값
            sig_a, thread_id(self.ticker, self.date, "analysts=market,news|asset=stock")
        )

    def test_different_signature_starts_fresh(self):
        """시그니처가 달라지면 기존 체크포인트를 무시하고 새로 시작하는지 검증하는 테스트."""
        global _should_crash
        builder = _build_graph()
        sig1 = "analysts=market,news,fundamentals|asset=stock"
        sig2 = "analysts=market|asset=stock"       # 애널리스트를 줄임 -> 다른 그래프 형태

        _should_crash = True
        tid1 = thread_id(self.ticker, self.date, sig1)
        with get_checkpointer(self.tmpdir, self.ticker) as saver:
            graph = builder.compile(checkpointer=saver)
            with self.assertRaises(RuntimeError):
                graph.invoke({"count": 0}, config={"configurable": {"thread_id": tid1}})

        self.assertTrue(has_checkpoint(self.tmpdir, self.ticker, self.date, sig1))
        # 다른 그래프 형태에는 재개할 체크포인트가 없어야 함.
        self.assertFalse(has_checkpoint(self.tmpdir, self.ticker, self.date, sig2))

        _should_crash = False
        tid2 = thread_id(self.ticker, self.date, sig2)
        self.assertNotEqual(tid1, tid2)
        with get_checkpointer(self.tmpdir, self.ticker) as saver:
            graph = builder.compile(checkpointer=saver)
            result = graph.invoke({"count": 0}, config={"configurable": {"thread_id": tid2}})
        self.assertEqual(result["count"], 11)
        # sig1의 체크포인트는 손대지 않은 채 그대로 남아 있음.
        self.assertTrue(has_checkpoint(self.tmpdir, self.ticker, self.date, sig1))

    def test_run_signature_captures_graph_shape(self):
        """실행 시그니처가 그래프 형태(자산 모드·애널리스트·토론 깊이)를 모두 반영하는지 검증하는 테스트."""
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        # 무거운 __init__ 없이 순수 헬퍼만 실행하기 위해 빈 인스턴스를 직접 생성합니다.
        g = object.__new__(TradingAgentsGraph)
        g.selected_analysts = ("market", "news")
        g.config = {"max_debate_rounds": 1, "max_risk_discuss_rounds": 1}
        base = g._run_signature("stock")

        self.assertNotEqual(base, g._run_signature("crypto"))     # 자산 모드(asset mode)
        g.selected_analysts = ("market",)
        self.assertNotEqual(base, g._run_signature("stock"))      # 애널리스트 선택
        g.selected_analysts = ("market", "news")
        g.config = {"max_debate_rounds": 3, "max_risk_discuss_rounds": 1}
        self.assertNotEqual(base, g._run_signature("stock"))      # 토론(debate) 깊이
        g.config = {"max_debate_rounds": 1, "max_risk_discuss_rounds": 5}
        self.assertNotEqual(base, g._run_signature("stock"))      # 리스크 논의 깊이
        # 동일한 입력에는 항상 같은 값이 나와야 함.
        g.config = {"max_debate_rounds": 1, "max_risk_discuss_rounds": 1}
        self.assertEqual(base, g._run_signature("stock"))


if __name__ == "__main__":
    unittest.main()
