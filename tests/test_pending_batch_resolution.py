# 이 파일은 설계 분석 중기 로드맵 #7 "pending 일괄 해소 경로"를 검증하는 테스트 모음입니다:
# (1) 실행 시작 시 현재 티커뿐 아니라 모든 티커의 pending 항목이 일괄 해소되는지,
# (2) resolve_pending_batch_limit 상한이 지켜지는지,
# (3) resolve_all_pending_on_run=False면 기존(현재 티커만) 동작인지,
# (4) 티커별 가격 조회 실패가 다른 항목의 해소를 막지 않는지(격리),
# (5) 오래됐는데도 가격 이력이 없는 항목이 unresolved로 마킹되어
#     재시도·past_context·스코어보드에서 제외되는지,
# (6) 같은 벤치마크 가격이 배치에서 한 번만 조회(캐싱)되는지.
"""pending 일괄 해소(설계분석 중기 로드맵 #7) 테스트 — 전 티커 해소, 상한, unresolved, 벤치마크 캐싱."""

import functools
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.eval import aggregate_entries
from tradingagents.graph.trading_graph import TradingAgentsGraph

_SEP = TradingMemoryLog._SEPARATOR

DECISION_BUY = "Rating: Buy\nEnter at $189-192, 6% portfolio cap."

# holding_days=5 확정에 충분한 6개 가격 바
PRICES_OK = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
BENCH_OK = [400.0, 401.0, 402.0, 403.0, 404.0, 405.0]


# ---------------------------------------------------------------------------
# 공용 헬퍼
# ---------------------------------------------------------------------------

def make_log(tmp_path, **extra):
    config = {"memory_log_path": str(tmp_path / "trading_memory.md"), **extra}
    return TradingMemoryLog(config)


def make_graph(log, **config_overrides):
    """실제 _fetch_returns/_resolve_benchmark/_resolve_pending_entries를
    바인딩한 mock 그래프와 mock 리플렉터를 만드는 헬퍼."""
    mock_reflector = MagicMock()
    mock_reflector.reflect_on_final_decision.return_value = "Lesson learned."
    mock_graph = MagicMock(spec=TradingAgentsGraph)
    mock_graph.memory_log = log
    mock_graph.reflector = mock_reflector
    mock_graph.config = {
        "holding_days": 5,
        "benchmark_ticker": None,
        "benchmark_map": {"": "SPY"},
        **config_overrides,
    }
    mock_graph._fetch_returns = functools.partial(
        TradingAgentsGraph._fetch_returns, mock_graph
    )
    mock_graph._resolve_benchmark = functools.partial(
        TradingAgentsGraph._resolve_benchmark, mock_graph
    )
    return mock_graph, mock_reflector


def _price_df(prices):
    """yfinance .history() 출력 형태에 맞춘 최소한의 DataFrame을 만드는 헬퍼."""
    return pd.DataFrame({"Close": prices})


def _counting_factory(prices_by_symbol, history_calls, raise_for=()):
    """심볼별 가격을 돌려주며 history() 호출 횟수를 기록하는 Ticker 팩토리.

    ``raise_for``에 포함된 심볼은 history() 호출 시 예외를 던진다(조회 실패
    시뮬레이션). 목록에 없는 심볼은 PRICES_OK를 기본으로 쓴다.
    """
    def _make_ticker(sym):
        m = MagicMock()

        def _history(start=None, end=None, _sym=sym):
            history_calls[_sym] = history_calls.get(_sym, 0) + 1
            if _sym in raise_for:
                raise ConnectionError(f"network down for {_sym}")
            return _price_df(prices_by_symbol.get(_sym, PRICES_OK))

        m.history.side_effect = _history
        return m
    return _make_ticker


def _resolve(mock_graph, ticker, prices_by_symbol, raise_for=()):
    """가격을 모킹한 상태로 _resolve_pending_entries를 실행하고 호출 횟수를 반환하는 헬퍼."""
    history_calls: dict[str, int] = {}
    factory = _counting_factory(prices_by_symbol, history_calls, raise_for=raise_for)
    with patch("yfinance.Ticker") as mock_ticker_cls:
        mock_ticker_cls.side_effect = factory
        TradingAgentsGraph._resolve_pending_entries(mock_graph, ticker)
    return history_calls


def _old_date(days=200):
    """unresolved 마킹 기준(holding_days x 6 달력일)을 확실히 넘긴 과거 날짜."""
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")


def _recent_date(days=2):
    """아직 데이터가 쌓일 수 있는 최근 날짜(조기 확정 가드 대상)."""
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# (1) 전 티커 일괄 해소
# ---------------------------------------------------------------------------

class TestResolveAllTickers:
    """기본 설정에서 다른 티커의 pending도 일괄 해소되는지 검증하는 테스트 묶음."""

    def test_other_ticker_pending_resolved_in_one_run(self, tmp_path):
        """NVDA 1회 실행으로 AAPL·MSFT의 pending까지 전부 해소되는지 검증하는 테스트."""
        log = make_log(tmp_path)
        log.store_decision("AAPL", "2026-01-05", DECISION_BUY)
        log.store_decision("MSFT", "2026-01-06", DECISION_BUY)
        log.store_decision("NVDA", "2026-01-07", DECISION_BUY)
        mock_graph, mock_reflector = make_graph(log)

        _resolve(mock_graph, "NVDA", {"SPY": BENCH_OK})

        assert log.get_pending_entries() == []
        entries = log.load_entries()
        assert len(entries) == 3
        assert all(e["pending"] is False for e in entries)
        assert all(e["reflection"] == "Lesson learned." for e in entries)
        assert mock_reflector.reflect_on_final_decision.call_count == 3

    def test_cross_ticker_lessons_available_after_batch(self, tmp_path):
        """일괄 해소 직후 다른 티커의 교훈이 cross-ticker 컨텍스트로 주입 가능한지 검증하는 테스트."""
        log = make_log(tmp_path)
        log.store_decision("AAPL", "2026-01-05", DECISION_BUY)
        mock_graph, _ = make_graph(log)

        _resolve(mock_graph, "NVDA", {"SPY": BENCH_OK})

        ctx = log.get_past_context("NVDA", asset_type="stock")
        assert "Recent cross-ticker lessons" in ctx
        assert "AAPL" in ctx


# ---------------------------------------------------------------------------
# (2) 배치 상한 (resolve_pending_batch_limit)
# ---------------------------------------------------------------------------

class TestBatchLimit:
    """한 실행에서 처리하는 pending 항목 수 상한을 검증하는 테스트 묶음."""

    def test_limit_caps_processed_entries(self, tmp_path):
        """limit=2면 3건 중 2건만 해소되고 나머지는 pending으로 남는지 검증하는 테스트."""
        log = make_log(tmp_path)
        log.store_decision("AAPL", "2026-01-05", DECISION_BUY)
        log.store_decision("MSFT", "2026-01-06", DECISION_BUY)
        log.store_decision("NVDA", "2026-01-07", DECISION_BUY)
        mock_graph, mock_reflector = make_graph(log, resolve_pending_batch_limit=2)

        _resolve(mock_graph, "NVDA", {"SPY": BENCH_OK})

        assert mock_reflector.reflect_on_final_decision.call_count == 2
        remaining = log.get_pending_entries()
        assert len(remaining) == 1
        # 현재 티커(NVDA) 우선 + 나머지는 오래된 순(AAPL) → MSFT가 이월된다.
        assert remaining[0]["ticker"] == "MSFT"

    def test_current_ticker_prioritised_within_limit(self, tmp_path):
        """limit=1이어도 현재 실행 티커의 항목이 가장 먼저 해소되는지 검증하는 테스트."""
        log = make_log(tmp_path)
        log.store_decision("AAPL", "2026-01-05", DECISION_BUY)  # 더 오래된 타 티커
        log.store_decision("NVDA", "2026-01-07", DECISION_BUY)
        mock_graph, _ = make_graph(log, resolve_pending_batch_limit=1)

        _resolve(mock_graph, "NVDA", {"SPY": BENCH_OK})

        remaining = log.get_pending_entries()
        assert [e["ticker"] for e in remaining] == ["AAPL"]
        resolved = [e for e in log.load_entries() if not e["pending"]]
        assert [e["ticker"] for e in resolved] == ["NVDA"]

    def test_zero_limit_means_unlimited(self, tmp_path):
        """limit=0(또는 None)이면 상한 없이 전부 처리되는지 검증하는 테스트."""
        log = make_log(tmp_path)
        for i, ticker in enumerate(["AAPL", "MSFT", "NVDA", "TSLA"]):
            log.store_decision(ticker, f"2026-01-{i+5:02d}", DECISION_BUY)
        mock_graph, _ = make_graph(log, resolve_pending_batch_limit=0)

        _resolve(mock_graph, "NVDA", {"SPY": BENCH_OK})

        assert log.get_pending_entries() == []


# ---------------------------------------------------------------------------
# (3) 설정 False → 기존 동작 (현재 티커만)
# ---------------------------------------------------------------------------

class TestOptOutLegacyBehavior:
    """resolve_all_pending_on_run=False에서 기존 동작이 보존되는지 검증하는 테스트 묶음."""

    def test_false_resolves_only_current_ticker(self, tmp_path):
        """False면 현재 티커만 해소되고 타 티커 pending은 그대로인지 검증하는 테스트."""
        log = make_log(tmp_path)
        log.store_decision("AAPL", "2026-01-05", DECISION_BUY)
        log.store_decision("NVDA", "2026-01-07", DECISION_BUY)
        mock_graph, _ = make_graph(log, resolve_all_pending_on_run=False)

        _resolve(mock_graph, "NVDA", {"SPY": BENCH_OK})

        remaining = log.get_pending_entries()
        assert [e["ticker"] for e in remaining] == ["AAPL"]
        resolved = [e for e in log.load_entries() if not e["pending"]]
        assert [e["ticker"] for e in resolved] == ["NVDA"]

    def test_false_with_no_current_ticker_pending_is_noop(self, tmp_path):
        """False + 현재 티커 pending 없음이면 가격 조회 자체가 없는지 검증하는 테스트."""
        log = make_log(tmp_path)
        log.store_decision("AAPL", "2026-01-05", DECISION_BUY)
        mock_graph, mock_reflector = make_graph(log, resolve_all_pending_on_run=False)

        history_calls = _resolve(mock_graph, "NVDA", {"SPY": BENCH_OK})

        assert history_calls == {}
        mock_reflector.reflect_on_final_decision.assert_not_called()
        assert len(log.get_pending_entries()) == 1


# ---------------------------------------------------------------------------
# (4) 가격 조회 실패 티커 격리
# ---------------------------------------------------------------------------

class TestFailureIsolation:
    """한 티커의 조회 실패가 다른 티커의 해소를 막지 않는지 검증하는 테스트 묶음."""

    def test_failed_ticker_skipped_others_resolved(self, tmp_path):
        """BAD 조회가 예외를 던져도 GOOD은 해소되고 BAD는 pending으로 남는지 검증하는 테스트."""
        log = make_log(tmp_path)
        log.store_decision("BAD", "2026-01-05", DECISION_BUY)
        log.store_decision("GOOD", "2026-01-06", DECISION_BUY)
        mock_graph, _ = make_graph(log)

        _resolve(mock_graph, "GOOD", {"SPY": BENCH_OK}, raise_for=("BAD",))

        remaining = log.get_pending_entries()
        assert [e["ticker"] for e in remaining] == ["BAD"]
        resolved = [e for e in log.load_entries() if not e["pending"]]
        assert [e["ticker"] for e in resolved] == ["GOOD"]

    def test_transient_error_never_marks_unresolved(self, tmp_path):
        """항목이 아무리 오래됐어도 '조회 오류'는 unresolved로 마킹되지 않는지
        검증하는 테스트 (일시적 네트워크 장애가 영구 마킹으로 이어지면 안 됨)."""
        log = make_log(tmp_path)
        log.store_decision("BAD", _old_date(), DECISION_BUY)
        mock_graph, _ = make_graph(log)

        _resolve(mock_graph, "BAD", {"SPY": BENCH_OK}, raise_for=("BAD",))

        entries = log.load_entries()
        assert entries[0]["pending"] is True
        assert entries[0]["unresolved"] is False


# ---------------------------------------------------------------------------
# (5) 해소 불가 항목 unresolved 마킹
# ---------------------------------------------------------------------------

class TestUnresolvedMarking:
    """가격 데이터가 영구히 없는 항목의 unresolved 마킹과 제외를 검증하는 테스트 묶음."""

    def _mark_dead_entry(self, tmp_path):
        """오래된 무(無)데이터 항목 하나를 unresolved로 마킹시키는 헬퍼."""
        log = make_log(tmp_path)
        log.store_decision("DEAD", _old_date(), DECISION_BUY)
        mock_graph, mock_reflector = make_graph(log)
        # 상장폐지 시뮬레이션: 결정일 이후 가격 바가 하나도 없음
        _resolve(mock_graph, "NVDA", {"DEAD": [], "SPY": BENCH_OK})
        return log, mock_reflector

    def test_stale_no_data_entry_marked_unresolved(self, tmp_path):
        """오래된 무데이터 항목이 unresolved로 마킹되고 pending에서 빠지는지 검증하는 테스트."""
        log, mock_reflector = self._mark_dead_entry(tmp_path)
        assert log.get_pending_entries() == []
        entry = log.load_entries()[0]
        assert entry["unresolved"] is True
        assert entry["pending"] is False
        assert entry["raw"] is None
        # 결정 본문은 기록으로 보존된다.
        assert entry["decision"] == DECISION_BUY.strip()
        mock_reflector.reflect_on_final_decision.assert_not_called()

    def test_recent_no_data_entry_stays_pending(self, tmp_path):
        """최근 결정은 데이터가 없어도(아직 쌓일 수 있음 — 조기 확정 가드)
        unresolved가 아니라 pending으로 남는지 검증하는 테스트."""
        log = make_log(tmp_path)
        log.store_decision("FRESH", _recent_date(), DECISION_BUY)
        mock_graph, _ = make_graph(log)

        _resolve(mock_graph, "FRESH", {"FRESH": [], "SPY": BENCH_OK})

        entries = log.load_entries()
        assert entries[0]["pending"] is True
        assert entries[0]["unresolved"] is False

    def test_stale_but_accumulating_entry_stays_pending(self, tmp_path):
        """오래된 항목이라도 가격 바가 존재(부족할 뿐)하면 unresolved가 아니라
        pending으로 남아 재시도되는지 검증하는 테스트 (가드와 마킹의 경계)."""
        log = make_log(tmp_path)
        log.store_decision("THIN", _old_date(), DECISION_BUY)
        mock_graph, _ = make_graph(log)

        # 2개 바 = 데이터는 있으나 holding_days(5)에 못 미침
        _resolve(mock_graph, "THIN", {"THIN": [100.0, 101.0], "SPY": BENCH_OK})

        entries = log.load_entries()
        assert entries[0]["pending"] is True
        assert entries[0]["unresolved"] is False

    def test_unresolved_not_retried_on_next_run(self, tmp_path):
        """마킹된 항목은 다음 실행의 재시도 루프(가격 조회)에서 제외되는지 검증하는 테스트."""
        log, _ = self._mark_dead_entry(tmp_path)
        mock_graph, _ = make_graph(log)
        history_calls = _resolve(mock_graph, "NVDA", {"SPY": BENCH_OK})
        assert history_calls == {}

    def test_unresolved_excluded_from_past_context(self, tmp_path):
        """unresolved 항목이 past_context(프롬프트 주입)에 나타나지 않는지 검증하는 테스트."""
        log, _ = self._mark_dead_entry(tmp_path)
        assert log.get_past_context("DEAD") == ""
        assert log.get_past_context("NVDA", asset_type="stock") == ""

    def test_unresolved_excluded_from_scoreboard(self, tmp_path):
        """unresolved 항목이 스코어보드 집계를 오염시키지 않는지 검증하는 테스트."""
        log, _ = self._mark_dead_entry(tmp_path)
        summary = aggregate_entries(log.load_entries())
        assert summary["total"] == 0
        assert summary["skipped"] == 1

    def test_store_decision_idempotent_for_unresolved(self, tmp_path):
        """unresolved로 마킹된 결정을 같은 날짜로 재저장해도 중복 pending이
        생기지 않는지 검증하는 테스트 (멱등성 가드)."""
        log, _ = self._mark_dead_entry(tmp_path)
        date = log.load_entries()[0]["date"]
        log.store_decision("DEAD", date, DECISION_BUY)
        assert len(log.load_entries()) == 1
        assert log.get_pending_entries() == []

    def test_outcome_update_ignores_unresolved(self, tmp_path):
        """update_with_outcome이 unresolved 항목을 pending으로 오인해 덮어쓰지
        않는지 검증하는 테스트."""
        log, _ = self._mark_dead_entry(tmp_path)
        date = log.load_entries()[0]["date"]
        log.update_with_outcome("DEAD", date, 0.05, 0.02, 5, "Should not apply.")
        entry = log.load_entries()[0]
        assert entry["unresolved"] is True
        assert entry["reflection"] == ""


# ---------------------------------------------------------------------------
# (6) 벤치마크 가격 캐싱
# ---------------------------------------------------------------------------

class TestBenchmarkCaching:
    """같은 벤치마크 가격이 배치에서 중복 조회되지 않는지 검증하는 테스트 묶음."""

    def test_same_benchmark_fetched_once(self, tmp_path):
        """티커 3개가 같은 벤치마크(SPY)를 쓸 때 SPY 조회가 1회인지 검증하는 테스트."""
        log = make_log(tmp_path)
        for i, ticker in enumerate(["AAPL", "MSFT", "NVDA"]):
            log.store_decision(ticker, f"2026-01-{i+5:02d}", DECISION_BUY)
        mock_graph, _ = make_graph(log)

        history_calls = _resolve(mock_graph, "NVDA", {"SPY": BENCH_OK})

        assert history_calls["SPY"] == 1
        # 종목은 각자 1회씩 조회된다.
        assert history_calls["AAPL"] == 1
        assert history_calls["MSFT"] == 1
        assert history_calls["NVDA"] == 1
        assert log.get_pending_entries() == []

    def test_cached_benchmark_sliced_per_entry_date(self, tmp_path):
        """결정일이 다른 항목들이 캐시된 벤치마크 이력을 각자의 결정일 이후
        구간으로 잘라 써서, 단건 조회와 동일한 알파가 나오는지 검증하는 테스트."""
        log = make_log(tmp_path)
        log.store_decision("AAA", "2026-03-02", DECISION_BUY)
        log.store_decision("BBB", "2026-03-04", DECISION_BUY)
        mock_graph, mock_reflector = make_graph(log)

        # 벤치마크 전체 이력: 2026-03-02부터 하루 1포인트씩 상승 (400, 401, ...)
        bench_full = pd.DataFrame(
            {"Close": [400.0 + i for i in range(15)]},
            index=pd.date_range("2026-03-02", periods=15, freq="D"),
        )
        history_calls: dict[str, int] = {}

        def _make_ticker(sym):
            m = MagicMock()

            def _history(start=None, end=None, _sym=sym):
                history_calls[_sym] = history_calls.get(_sym, 0) + 1
                if _sym == "SPY":
                    # 실제 yfinance처럼 요청한 start 이후 구간만 반환
                    return bench_full.loc[start:]
                return pd.DataFrame(
                    {"Close": PRICES_OK},
                    index=pd.date_range(start, periods=len(PRICES_OK), freq="D"),
                )

            m.history.side_effect = _history
            return m

        with patch("yfinance.Ticker") as mock_ticker_cls:
            mock_ticker_cls.side_effect = _make_ticker
            TradingAgentsGraph._resolve_pending_entries(mock_graph, "AAA")

        assert history_calls["SPY"] == 1
        assert log.get_pending_entries() == []

        calls = mock_reflector.reflect_on_final_decision.call_args_list
        assert len(calls) == 2
        # 처리 순서: 현재 티커 AAA(2026-03-02) → BBB(2026-03-04)
        raw_expected = (PRICES_OK[5] - PRICES_OK[0]) / PRICES_OK[0]
        # AAA: 벤치마크 400 → 405 (03-02부터 5거래일)
        assert calls[0].kwargs["raw_return"] == pytest.approx(raw_expected)
        assert calls[0].kwargs["alpha_return"] == pytest.approx(
            raw_expected - 5.0 / 400.0
        )
        # BBB: 캐시를 03-04 이후로 잘라 402 → 407이어야 한다.
        # (자르지 않으면 400 → 405로 잘못 계산된다.)
        assert calls[1].kwargs["raw_return"] == pytest.approx(raw_expected)
        assert calls[1].kwargs["alpha_return"] == pytest.approx(
            raw_expected - 5.0 / 402.0
        )


# ---------------------------------------------------------------------------
# 메모리 로그 단위: unresolved 태그의 파싱·왕복
# ---------------------------------------------------------------------------

class TestUnresolvedTagParsing:
    """batch_mark_unresolved와 unresolved 태그 파싱을 검증하는 테스트 묶음."""

    def test_mark_unresolved_rewrites_tag(self, tmp_path):
        """pending 태그가 `| unresolved]`로 교체되고 본문이 보존되는지 검증하는 테스트."""
        log = make_log(tmp_path)
        log.store_decision("DEAD", "2025-01-05", DECISION_BUY)
        log.batch_mark_unresolved([{"ticker": "DEAD", "trade_date": "2025-01-05"}])
        raw = (tmp_path / "trading_memory.md").read_text(encoding="utf-8")
        assert "[2025-01-05 | DEAD | Buy | unresolved]" in raw
        assert "| pending]" not in raw
        assert DECISION_BUY.strip() in raw

    def test_mark_unresolved_only_touches_matching_entry(self, tmp_path):
        """마킹이 지정한 (티커, 날짜) 항목만 바꾸고 다른 pending은 유지하는지 검증하는 테스트."""
        log = make_log(tmp_path)
        log.store_decision("DEAD", "2025-01-05", DECISION_BUY)
        log.store_decision("LIVE", "2025-01-05", DECISION_BUY)
        log.batch_mark_unresolved([{"ticker": "DEAD", "trade_date": "2025-01-05"}])
        pending = log.get_pending_entries()
        assert [e["ticker"] for e in pending] == ["LIVE"]

    def test_mark_unresolved_missing_entry_is_noop(self, tmp_path):
        """존재하지 않는 항목의 마킹 요청은 로그를 건드리지 않는지 검증하는 테스트."""
        log = make_log(tmp_path)
        log.store_decision("LIVE", "2025-01-05", DECISION_BUY)
        before = (tmp_path / "trading_memory.md").read_text(encoding="utf-8")
        log.batch_mark_unresolved([{"ticker": "GHOST", "trade_date": "2025-01-05"}])
        after = (tmp_path / "trading_memory.md").read_text(encoding="utf-8")
        assert before == after

    def test_resolved_entries_not_flagged_unresolved(self, tmp_path):
        """정상 확정 항목의 파싱에서 unresolved 플래그가 False인지 검증하는 테스트 (하위 호환)."""
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-05", DECISION_BUY)
        log.update_with_outcome("NVDA", "2026-01-05", 0.05, 0.02, 5, "Good call.")
        entry = log.load_entries()[0]
        assert entry["unresolved"] is False
        assert entry["pending"] is False
        assert entry["raw"] == "+5.0%"

    def test_legacy_pending_entry_unaffected(self, tmp_path):
        """구형(파일 직접 기록) pending 항목이 여전히 pending으로 파싱되는지 검증하는 테스트."""
        entry = (
            "[2026-01-05 | NVDA | Buy | pending]\n\n"
            f"DECISION:\n{DECISION_BUY}"
            + _SEP
        )
        (tmp_path / "trading_memory.md").write_text(entry, encoding="utf-8")
        log = make_log(tmp_path)
        parsed = log.load_entries()[0]
        assert parsed["pending"] is True
        assert parsed["unresolved"] is False
