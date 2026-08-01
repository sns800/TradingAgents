# 이 파일은 일괄 백테스트 로직을 검증하는 테스트 모음입니다. 가격 데이터를
# 모킹해 복수 보유기간(1/5/20 거래일) 수익률·알파 계산을 결정론적으로 확인하고,
# 개별 실행 실패가 배치 전체를 죽이지 않는 격리, 스케줄 생성, 벤치마크 결정,
# JSONL 기록, 요약 표 렌더링을 검증합니다. LLM·네트워크 호출은 없습니다.
"""백테스트 테스트 — 다중 보유기간 수익률, 실패 격리, 스케줄·요약."""

import json
from unittest.mock import patch

import pandas as pd
import pytest

from tradingagents.eval.backtest import (
    annotate_returns,
    build_schedule,
    compute_holding_returns,
    make_decision_fn,
    resolve_benchmark,
    run_backtest,
    summarize_records,
    write_jsonl,
)

# ---------------------------------------------------------------------------
# 공용 헬퍼 — 가짜 가격 조회
# ---------------------------------------------------------------------------


def _make_fetch(prices_by_symbol):
    """심볼별 고정 종가 시계열을 반환하는 가짜 가격 조회 함수를 만든다."""

    def fetch(symbol, start, end):
        return pd.Series(prices_by_symbol[symbol], dtype=float)

    return fetch


# 종목은 매 거래일 +1씩 상승(기준가 100), 벤치마크는 평평(알파 = 원수익률)
RISING_STOCK = [100.0 + i for i in range(25)]
FLAT_BENCH = [100.0] * 25


# ---------------------------------------------------------------------------
# compute_holding_returns — 복수 보유기간 수익률
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_multi_horizon_returns_and_alpha():
    fetch = _make_fetch({"AAPL": RISING_STOCK, "SPY": FLAT_BENCH})
    returns = compute_holding_returns("AAPL", "2025-01-06", "SPY", fetch_history=fetch)

    assert returns["1d"]["raw"] == pytest.approx(0.01)
    assert returns["5d"]["raw"] == pytest.approx(0.05)
    assert returns["20d"]["raw"] == pytest.approx(0.20)
    # 벤치마크가 평평하므로 알파 == 원수익률
    for key in ("1d", "5d", "20d"):
        assert returns[key]["alpha"] == pytest.approx(returns[key]["raw"])


@pytest.mark.unit
def test_alpha_subtracts_benchmark_return():
    # 벤치마크도 상승하면 알파는 원수익률 - 벤치마크 수익률
    bench = [100.0 + 0.5 * i for i in range(25)]
    fetch = _make_fetch({"AAPL": RISING_STOCK, "SPY": bench})
    returns = compute_holding_returns("AAPL", "2025-01-06", "SPY", fetch_history=fetch)
    assert returns["5d"]["raw"] == pytest.approx(0.05)
    assert returns["5d"]["alpha"] == pytest.approx(0.05 - 0.025)


@pytest.mark.unit
def test_insufficient_data_leaves_longer_horizons_none():
    # 거래일 4개(결정일 + 3일)만 있으면 1d만 계산되고 5d/20d는 None
    fetch = _make_fetch({"AAPL": RISING_STOCK[:4], "SPY": FLAT_BENCH[:4]})
    returns = compute_holding_returns("AAPL", "2025-01-06", "SPY", fetch_history=fetch)
    assert returns["1d"]["raw"] == pytest.approx(0.01)
    assert returns["5d"] == {"raw": None, "alpha": None}
    assert returns["20d"] == {"raw": None, "alpha": None}


@pytest.mark.unit
def test_benchmark_shorter_than_stock_limits_horizons():
    # 종목 데이터가 충분해도 벤치마크가 짧으면 해당 보유기간은 None
    fetch = _make_fetch({"AAPL": RISING_STOCK, "SPY": FLAT_BENCH[:4]})
    returns = compute_holding_returns("AAPL", "2025-01-06", "SPY", fetch_history=fetch)
    assert returns["1d"]["raw"] is not None
    assert returns["5d"]["raw"] is None


@pytest.mark.unit
def test_fetch_failure_returns_all_none_without_raising():
    def broken_fetch(symbol, start, end):
        raise ConnectionError("network down")

    returns = compute_holding_returns(
        "AAPL", "2025-01-06", "SPY", fetch_history=broken_fetch
    )
    assert all(v == {"raw": None, "alpha": None} for v in returns.values())


# ---------------------------------------------------------------------------
# build_schedule — 거래일 간격 스케줄
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_schedule_every_5_business_days():
    # 2025-01-06(월) ~ 2025-01-31(금): 영업일 20개 → 5일 간격이면 4개 날짜
    schedule = build_schedule(["SPY", "AAPL"], "2025-01-06", "2025-01-31", every=5)
    dates = sorted({d for _, d in schedule})
    assert dates == ["2025-01-06", "2025-01-13", "2025-01-20", "2025-01-27"]
    assert len(schedule) == 8  # 티커 2개 × 날짜 4개
    # 날짜 오름차순(같은 날짜의 티커들이 인접)으로 배치되어야 한다
    assert schedule[0] == ("SPY", "2025-01-06")
    assert schedule[1] == ("AAPL", "2025-01-06")


@pytest.mark.unit
def test_build_schedule_rejects_invalid_every():
    with pytest.raises(ValueError):
        build_schedule(["SPY"], "2025-01-06", "2025-01-31", every=0)


# ---------------------------------------------------------------------------
# resolve_benchmark — 벤치마크 결정 규칙
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_benchmark_by_suffix_and_default():
    assert resolve_benchmark("7203.T") == "^N225"
    assert resolve_benchmark("AAPL") == "SPY"


@pytest.mark.unit
def test_resolve_benchmark_explicit_override():
    config = {"benchmark_ticker": "QQQ", "benchmark_map": {"": "SPY"}}
    assert resolve_benchmark("AAPL", config) == "QQQ"


# ---------------------------------------------------------------------------
# run_backtest — 실패 격리
# ---------------------------------------------------------------------------


def _decision_fn(ticker, trade_date):
    if ticker == "BAD":
        raise RuntimeError("provider exploded")
    return {"rating": "Buy", "decision": f"Rating: Buy for {ticker} on {trade_date}"}


@pytest.mark.unit
def test_run_backtest_isolates_failures():
    schedule = [
        ("AAPL", "2025-01-06"),
        ("BAD", "2025-01-06"),
        ("MSFT", "2025-01-06"),
    ]
    records = run_backtest(schedule, _decision_fn, mode="full")

    assert len(records) == 3  # 실패해도 다음 조합을 계속 실행
    ok = [r for r in records if r["status"] == "ok"]
    errors = [r for r in records if r["status"] == "error"]
    assert [r["ticker"] for r in ok] == ["AAPL", "MSFT"]
    assert errors[0]["ticker"] == "BAD"
    assert "RuntimeError: provider exploded" in errors[0]["error"]
    assert "rating" not in errors[0]


@pytest.mark.unit
def test_run_backtest_truncates_long_decisions():
    def verbose_fn(ticker, trade_date):
        return {"rating": "Hold", "decision": "x" * 10_000}

    records = run_backtest([("AAPL", "2025-01-06")], verbose_fn, mode="full")
    assert len(records[0]["decision"]) == 2000


# ---------------------------------------------------------------------------
# annotate_returns — 사후 채점 병기
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_annotate_returns_skips_error_records():
    fetch = _make_fetch({"AAPL": RISING_STOCK, "SPY": FLAT_BENCH})
    records = [
        {"ticker": "AAPL", "trade_date": "2025-01-06", "mode": "full",
         "rating": "Buy", "status": "ok"},
        {"ticker": "BAD", "trade_date": "2025-01-06", "mode": "full",
         "status": "error", "error": "RuntimeError: boom"},
    ]
    annotate_returns(records, fetch_history=fetch)

    assert records[0]["benchmark"] == "SPY"
    assert records[0]["returns"]["5d"]["alpha"] == pytest.approx(0.05)
    assert "returns" not in records[1]


# ---------------------------------------------------------------------------
# write_jsonl / summarize_records
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_write_jsonl_roundtrip(tmp_path):
    records = [
        {"ticker": "AAPL", "trade_date": "2025-01-06", "status": "ok",
         "returns": {"1d": {"raw": 0.01, "alpha": 0.01}}},
        {"ticker": "BAD", "trade_date": "2025-01-06", "status": "error",
         "error": "RuntimeError: boom"},
    ]
    path = write_jsonl(records, tmp_path / "out" / "backtest.jsonl")
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert [json.loads(line) for line in lines] == records


@pytest.mark.unit
def test_summarize_records_hit_rate_and_alpha():
    def _rec(mode, rating, alpha_5d):
        return {
            "ticker": "AAPL", "trade_date": "2025-01-06", "mode": mode,
            "rating": rating, "status": "ok",
            "returns": {
                "1d": {"raw": None, "alpha": None},
                "5d": {"raw": alpha_5d, "alpha": alpha_5d},
                "20d": {"raw": None, "alpha": None},
            },
        }

    records = [
        _rec("full", "Buy", 0.02),          # 적중
        _rec("full", "Buy", -0.02),         # 미적중
        _rec("single_llm", "Sell", -0.03),  # 적중
        {"ticker": "BAD", "trade_date": "2025-01-06", "mode": "full",
         "status": "error", "error": "boom"},
    ]
    markdown = summarize_records(records, hold_threshold=0.01)

    assert "실패: 1건" in markdown
    assert "| full | Buy | 2 | 50.0% |" in markdown
    assert "| single_llm | Sell | 1 | 100.0% |" in markdown
    # 수익률 없는 보유기간은 n/a로 표기
    assert "n/a" in markdown


@pytest.mark.unit
def test_summarize_records_pending_returns_excluded_from_hit_rate():
    # 5d 알파가 아직 None(너무 최근)이면 적중률 분모에서 제외 → n/a
    records = [{
        "ticker": "AAPL", "trade_date": "2025-01-06", "mode": "full",
        "rating": "Buy", "status": "ok",
        "returns": {
            "1d": {"raw": 0.01, "alpha": 0.01},
            "5d": {"raw": None, "alpha": None},
            "20d": {"raw": None, "alpha": None},
        },
    }]
    markdown = summarize_records(records, hold_threshold=0.01)
    assert "| full | Buy | 1 | n/a |" in markdown


# ---------------------------------------------------------------------------
# make_decision_fn — 모드 라우팅 (무거운 의존성은 모킹)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_make_decision_fn_rejects_unknown_mode():
    with pytest.raises(ValueError):
        make_decision_fn("hybrid")


@pytest.mark.unit
def test_make_decision_fn_single_llm_uses_baseline():
    fake_llm = object()
    with (
        patch(
            "tradingagents.eval.baseline.create_baseline_llm", return_value=fake_llm
        ) as mock_create,
        patch(
            "tradingagents.eval.baseline.run_single_llm_baseline",
            return_value={"rating": "Buy", "response": "Rating: Buy", "ticker": "AAPL",
                          "trade_date": "2025-01-06", "context": ""},
        ) as mock_run,
    ):
        fn = make_decision_fn("single_llm", depth=2)
        out = fn("AAPL", "2025-01-06")

    assert out == {"rating": "Buy", "decision": "Rating: Buy"}
    mock_create.assert_called_once()  # LLM은 배치당 한 번만 생성
    call_kwargs = mock_run.call_args.kwargs
    assert call_kwargs["llm"] is fake_llm
    # depth 오버라이드가 config에 반영되어야 한다
    assert call_kwargs["config"]["max_debate_rounds"] == 2
    assert call_kwargs["config"]["max_risk_discuss_rounds"] == 2
