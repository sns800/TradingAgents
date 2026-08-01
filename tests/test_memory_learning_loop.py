# 이 파일은 설계 분석 로드맵의 메모리·학습 루프 개선 두 가지를 검증하는 테스트 모음입니다:
# (A) 반성 조기 확정 가드 — holding_days만큼 거래일이 쌓이기 전에는 항목을
#     pending으로 유지하고, 확정 시 실제 보유일(actual_days)을 리플렉션에 전달.
# (B) past_context 축약(태그 + REFLECTION 전문 + DECISION 앞 300자) 및
#     cross-ticker 교훈의 자산군(stock/crypto) 필터, 구형 항목 하위 호환.
"""메모리·학습 루프 가드 테스트 — 조기 확정 방지, past_context 축약, 자산군 필터."""

import functools
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.graph.reflection import Reflector
from tradingagents.graph.trading_graph import TradingAgentsGraph

_SEP = TradingMemoryLog._SEPARATOR

DECISION_BUY = "Rating: Buy\nEnter at $189-192, 6% portfolio cap."
# 300자 절단 검증용 장문 결정문 (약 1000자)
DECISION_LONG = "Rating: Hold\n" + "Detailed thesis sentence about market structure. " * 20


# ---------------------------------------------------------------------------
# 공용 헬퍼
# ---------------------------------------------------------------------------

def make_log(tmp_path, **extra):
    config = {"memory_log_path": str(tmp_path / "trading_memory.md"), **extra}
    return TradingMemoryLog(config)


def _price_df(prices):
    """yfinance .history() 출력 형태에 맞춘 최소한의 DataFrame을 만드는 헬퍼."""
    return pd.DataFrame({"Close": prices})


def _ticker_factory(stock_prices, bench_prices):
    """심볼에 따라 종목/벤치마크 가격을 돌려주는 yfinance.Ticker 대체 팩토리."""
    def _make_ticker(sym):
        m = MagicMock()
        m.history.return_value = _price_df(
            bench_prices if sym == "SPY" else stock_prices
        )
        return m
    return _make_ticker


def _resolve_entry(log, ticker, date, decision, reflection, asset_type="stock"):
    """결정을 저장한 뒤 API를 통해 즉시 결과를 확정(resolve)하는 헬퍼."""
    log.store_decision(ticker, date, decision, asset_type=asset_type)
    log.update_with_outcome(ticker, date, 0.05, 0.02, 5, reflection)


def _seed_legacy_completed(tmp_path, ticker, date, decision_text, reflection_text):
    """ASSET 태그가 없는 구형(legacy) 완료 항목을 파일에 직접 기록하는 헬퍼."""
    entry = (
        f"[{date} | {ticker} | Buy | +1.0% | +0.5% | 5d]\n\n"
        f"DECISION:\n{decision_text}\n\n"
        f"REFLECTION:\n{reflection_text}"
        + _SEP
    )
    with open(tmp_path / "trading_memory.md", "a", encoding="utf-8") as f:
        f.write(entry)


def _make_resolving_graph(log, holding_days=5):
    """실제 _fetch_returns/_resolve_benchmark를 바인딩한 mock 그래프를 만드는 헬퍼."""
    mock_reflector = MagicMock()
    mock_reflector.reflect_on_final_decision.return_value = "Lesson learned."
    mock_graph = MagicMock(spec=TradingAgentsGraph)
    mock_graph.memory_log = log
    mock_graph.reflector = mock_reflector
    mock_graph.config = {
        "holding_days": holding_days,
        "benchmark_ticker": None,
        "benchmark_map": {"": "SPY"},
    }
    mock_graph._fetch_returns = functools.partial(
        TradingAgentsGraph._fetch_returns, mock_graph
    )
    mock_graph._resolve_benchmark = functools.partial(
        TradingAgentsGraph._resolve_benchmark, mock_graph
    )
    return mock_graph, mock_reflector


# ---------------------------------------------------------------------------
# 항목 A: 반성 조기 확정 가드
# ---------------------------------------------------------------------------

class TestEarlyResolutionGuard:
    """holding_days만큼 거래일이 쌓이기 전에는 결과를 확정하지 않는지 검증하는 테스트 묶음."""

    def test_fetch_returns_defers_on_next_day_rerun(self):
        """결정 다음 거래일 데이터만 있으면(가격 바 2개) 1일 수익률로 조기
        확정하지 않고 (None, None, None)을 반환하는지 검증하는 테스트."""
        mock_graph = MagicMock(spec=TradingAgentsGraph)
        with patch("yfinance.Ticker") as mock_ticker_cls:
            mock_ticker_cls.side_effect = _ticker_factory(
                [100.0, 102.0], [400.0, 402.0]
            )
            raw, alpha, days = TradingAgentsGraph._fetch_returns(
                mock_graph, "NVDA", "2026-01-05"
            )
        assert raw is None and alpha is None and days is None

    def test_fetch_returns_defers_below_holding_days(self):
        """거래일이 holding_days보다 하루라도 모자라면(4 < 5) 확정하지 않는지 검증하는 테스트."""
        mock_graph = MagicMock(spec=TradingAgentsGraph)
        with patch("yfinance.Ticker") as mock_ticker_cls:
            mock_ticker_cls.side_effect = _ticker_factory(
                [100.0, 101.0, 102.0, 103.0, 104.0],
                [400.0, 401.0, 402.0, 403.0, 404.0],
            )
            raw, alpha, days = TradingAgentsGraph._fetch_returns(
                mock_graph, "NVDA", "2026-01-05"
            )
        assert raw is None and alpha is None and days is None

    def test_fetch_returns_resolves_at_exact_holding_days(self):
        """거래일이 정확히 holding_days만큼 쌓이면 그 기간의 수익률로 확정하는지 검증하는 테스트."""
        mock_graph = MagicMock(spec=TradingAgentsGraph)
        with patch("yfinance.Ticker") as mock_ticker_cls:
            mock_ticker_cls.side_effect = _ticker_factory(
                [100.0, 102.0, 104.0, 103.0, 105.0, 106.0],
                [400.0, 402.0, 404.0, 403.0, 405.0, 406.0],
            )
            raw, alpha, days = TradingAgentsGraph._fetch_returns(
                mock_graph, "NVDA", "2026-01-05"
            )
        assert days == 5
        assert raw == pytest.approx(0.06)
        assert alpha == pytest.approx(0.06 - 6.0 / 400.0)

    def test_fetch_returns_respects_custom_holding_days(self):
        """holding_days 설정을 줄이면(2일) 그만큼의 데이터만으로도 확정되는지 검증하는 테스트."""
        mock_graph = MagicMock(spec=TradingAgentsGraph)
        with patch("yfinance.Ticker") as mock_ticker_cls:
            mock_ticker_cls.side_effect = _ticker_factory(
                [100.0, 102.0, 104.0], [400.0, 401.0, 402.0]
            )
            raw, alpha, days = TradingAgentsGraph._fetch_returns(
                mock_graph, "NVDA", "2026-01-05", holding_days=2
            )
        assert days == 2
        assert raw == pytest.approx(0.04)

    def test_entry_stays_pending_until_holding_days(self, tmp_path):
        """다음 거래일 재실행 시 항목이 pending으로 남고 리플렉션이 호출되지
        않으며, holding_days만큼 데이터가 쌓인 재실행에서 확정되는지 검증하는 테스트."""
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-05", DECISION_BUY)
        mock_graph, mock_reflector = _make_resolving_graph(log)

        # 1단계: 결정 다음 거래일 데이터만 존재 → pending 유지
        with patch("yfinance.Ticker") as mock_ticker_cls:
            mock_ticker_cls.side_effect = _ticker_factory(
                [100.0, 101.0], [400.0, 401.0]
            )
            TradingAgentsGraph._resolve_pending_entries(mock_graph, "NVDA")
        assert len(log.get_pending_entries()) == 1
        mock_reflector.reflect_on_final_decision.assert_not_called()

        # 2단계: holding_days(5)만큼 거래일이 쌓임 → 확정
        with patch("yfinance.Ticker") as mock_ticker_cls:
            mock_ticker_cls.side_effect = _ticker_factory(
                [100.0, 101.0, 102.0, 103.0, 104.0, 105.0],
                [400.0, 401.0, 402.0, 403.0, 404.0, 405.0],
            )
            TradingAgentsGraph._resolve_pending_entries(mock_graph, "NVDA")
        assert log.get_pending_entries() == []
        entries = log.load_entries()
        assert len(entries) == 1
        assert entries[0]["pending"] is False
        assert entries[0]["holding"] == "5d"
        assert entries[0]["reflection"] == "Lesson learned."

    def test_resolve_passes_actual_days_to_reflector(self, tmp_path):
        """확정 시 실제 보유일(actual_days)이 리플렉션 입력으로 전달되는지 검증하는 테스트."""
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-05", DECISION_BUY)
        mock_reflector = MagicMock()
        mock_reflector.reflect_on_final_decision.return_value = "Lesson."
        mock_graph = MagicMock(spec=TradingAgentsGraph)
        mock_graph.memory_log = log
        mock_graph.reflector = mock_reflector
        mock_graph.config = {"holding_days": 5}
        mock_graph._fetch_returns = MagicMock(return_value=(0.05, 0.02, 5))
        TradingAgentsGraph._resolve_pending_entries(mock_graph, "NVDA")
        kwargs = mock_reflector.reflect_on_final_decision.call_args.kwargs
        assert kwargs["actual_days"] == 5
        # 설정된 holding_days가 수익률 조회에도 전달되는지 확인
        fetch_kwargs = mock_graph._fetch_returns.call_args.kwargs
        assert fetch_kwargs["holding_days"] == 5

    # Reflector 프롬프트의 보유 기간 표기

    def test_reflector_prompt_includes_holding_period(self):
        """actual_days를 넘기면 LLM 입력에 보유 거래일 수가 포함되는지 검증하는 테스트."""
        mock_llm = MagicMock()
        mock_llm.invoke.return_value.content = "ok"
        reflector = Reflector(mock_llm)
        reflector.reflect_on_final_decision(
            final_decision=DECISION_BUY,
            raw_return=0.05,
            alpha_return=0.02,
            actual_days=5,
        )
        messages = mock_llm.invoke.call_args[0][0]
        human_content = next(content for role, content in messages if role == "human")
        assert "Holding period: 5 trading days" in human_content

    def test_reflector_omits_holding_period_when_unknown(self):
        """actual_days를 넘기지 않는 기존 호출자는 보유 기간 줄이 생략되는지 검증하는 테스트."""
        mock_llm = MagicMock()
        mock_llm.invoke.return_value.content = "ok"
        reflector = Reflector(mock_llm)
        reflector.reflect_on_final_decision(
            final_decision=DECISION_BUY, raw_return=0.05, alpha_return=0.02
        )
        messages = mock_llm.invoke.call_args[0][0]
        human_content = next(content for role, content in messages if role == "human")
        assert "Holding period" not in human_content


# ---------------------------------------------------------------------------
# 항목 B-1: past_context 축약 (태그 + REFLECTION 전문 + DECISION 앞 300자)
# ---------------------------------------------------------------------------

class TestPastContextCondensation:
    """같은 티커 과거 항목이 축약 형식으로 주입되는지 검증하는 테스트 묶음."""

    def test_long_decision_truncated_reflection_kept(self, tmp_path):
        """장문 DECISION은 앞 300자로 절단되고 REFLECTION은 전문이 유지되는지 검증하는 테스트."""
        log = make_log(tmp_path)
        _resolve_entry(log, "NVDA", "2026-01-05", DECISION_LONG, "Key lesson intact.")
        ctx = log.get_past_context("NVDA")
        assert "Key lesson intact." in ctx
        assert "REFLECTION:" in ctx
        assert "DECISION:" in ctx
        decision_body = DECISION_LONG.strip()
        assert decision_body[:100] in ctx          # 앞부분은 포함
        assert decision_body not in ctx            # 전문은 미포함
        assert "..." in ctx                        # 절단 마커

    def test_short_decision_not_truncated(self, tmp_path):
        """300자 이하의 짧은 DECISION은 그대로 주입되는지 검증하는 테스트."""
        log = make_log(tmp_path)
        _resolve_entry(log, "NVDA", "2026-01-05", DECISION_BUY, "Correct call.")
        ctx = log.get_past_context("NVDA")
        assert DECISION_BUY.strip() in ctx

    def test_reflection_precedes_decision_snippet(self, tmp_path):
        """교훈(REFLECTION)이 결정문 요약보다 앞에 오는지 검증하는 테스트."""
        log = make_log(tmp_path)
        _resolve_entry(log, "NVDA", "2026-01-05", DECISION_LONG, "Lesson first.")
        ctx = log.get_past_context("NVDA")
        assert ctx.index("REFLECTION:") < ctx.index("DECISION:")

    def test_truncation_is_deterministic(self, tmp_path):
        """같은 로그에 대해 past_context가 항상 동일한지(결정론적 절단) 검증하는 테스트."""
        log = make_log(tmp_path)
        _resolve_entry(log, "NVDA", "2026-01-05", DECISION_LONG, "Stable lesson.")
        assert log.get_past_context("NVDA") == log.get_past_context("NVDA")

    def test_asset_line_not_injected(self, tmp_path):
        """내부 저장용 ASSET 태그 줄이 프롬프트 컨텍스트에 노출되지 않는지 검증하는 테스트."""
        log = make_log(tmp_path)
        _resolve_entry(log, "NVDA", "2026-01-05", DECISION_BUY, "Clean.", asset_type="stock")
        _resolve_entry(log, "AAPL", "2026-01-06", DECISION_BUY, "Cross.", asset_type="stock")
        ctx = log.get_past_context("NVDA")
        assert "ASSET:" not in ctx


# ---------------------------------------------------------------------------
# 항목 B-2: cross-ticker 자산군 필터 + 구형 항목 하위 호환
# ---------------------------------------------------------------------------

class TestCrossTickerAssetFilter:
    """cross-ticker 교훈이 같은 자산군에서만 선별되는지 검증하는 테스트 묶음."""

    def _mixed_log(self, tmp_path):
        """주식/크립토가 섞인 확정 로그를 만드는 헬퍼."""
        log = make_log(tmp_path)
        _resolve_entry(log, "AAPL", "2026-01-05", DECISION_BUY, "Stock lesson.", asset_type="stock")
        _resolve_entry(log, "BTC-USD", "2026-01-06", DECISION_BUY, "Crypto lesson.", asset_type="crypto")
        return log

    def test_stock_run_excludes_crypto_lessons(self, tmp_path):
        """주식 분석의 cross-ticker 섹션에 crypto 교훈이 주입되지 않는지 검증하는 테스트."""
        log = self._mixed_log(tmp_path)
        ctx = log.get_past_context("NVDA", asset_type="stock")
        assert "Stock lesson." in ctx
        assert "Crypto lesson." not in ctx

    def test_crypto_run_excludes_stock_lessons(self, tmp_path):
        """크립토 분석의 cross-ticker 섹션에 주식 교훈이 주입되지 않는지 검증하는 테스트."""
        log = self._mixed_log(tmp_path)
        ctx = log.get_past_context("ETH-USD", asset_type="crypto")
        assert "Crypto lesson." in ctx
        assert "Stock lesson." not in ctx

    def test_same_ticker_section_not_asset_filtered(self, tmp_path):
        """같은 티커 항목은 asset_type 인자와 무관하게 항상 주입되는지 검증하는 테스트."""
        log = self._mixed_log(tmp_path)
        ctx = log.get_past_context("BTC-USD", asset_type="crypto")
        assert "Past analyses of BTC-USD" in ctx
        assert "Crypto lesson." in ctx

    def test_legacy_untagged_entries_treated_as_stock(self, tmp_path):
        """ASSET 태그가 없는 구형 항목은 stock으로 간주되는지 검증하는 테스트 (하위 호환)."""
        log = make_log(tmp_path)
        _seed_legacy_completed(tmp_path, "AAPL", "2026-01-05", "Buy AAPL.", "Legacy lesson.")
        assert log.load_entries()[0]["asset_type"] == "stock"
        # 주식 실행에는 포함되고, 크립토 실행에서는 제외된다.
        assert "Legacy lesson." in log.get_past_context("NVDA", asset_type="stock")
        assert "Legacy lesson." not in log.get_past_context("ETH-USD", asset_type="crypto")

    def test_store_decision_asset_tag_roundtrip(self, tmp_path):
        """store_decision의 asset_type이 저장·파싱을 왕복해도 유지되는지 검증하는 테스트."""
        log = make_log(tmp_path)
        log.store_decision("BTC-USD", "2026-01-05", DECISION_BUY, asset_type="crypto")
        log.store_decision("NVDA", "2026-01-06", DECISION_BUY)
        entries = log.load_entries()
        assert entries[0]["asset_type"] == "crypto"
        assert entries[1]["asset_type"] == "stock"  # 기본값

    def test_asset_tag_survives_outcome_update(self, tmp_path):
        """결과 확정(update_with_outcome) 후에도 ASSET 태그와 본문이 보존되는지 검증하는 테스트."""
        log = make_log(tmp_path)
        log.store_decision("BTC-USD", "2026-01-05", DECISION_BUY, asset_type="crypto")
        log.update_with_outcome("BTC-USD", "2026-01-05", 0.05, 0.02, 5, "Held up.")
        e = log.load_entries()[0]
        assert e["pending"] is False
        assert e["asset_type"] == "crypto"
        assert e["decision"] == DECISION_BUY.strip()
        assert e["reflection"] == "Held up."


# ---------------------------------------------------------------------------
# 항목 B-3: 로그 로테이션 기본값 (무한 성장 방지)
# ---------------------------------------------------------------------------

class TestRotationFiniteDefault:
    """memory_log_max_entries 기본값이 유한하고 로테이션과 연동되는지 검증하는 테스트 묶음."""

    def test_default_config_cap_applies_to_log(self, tmp_path):
        """DEFAULT_CONFIG의 상한이 TradingMemoryLog 로테이션에 실제로 적용되는지 검증하는 테스트."""
        from tradingagents.default_config import DEFAULT_CONFIG
        assert DEFAULT_CONFIG["memory_log_max_entries"] == 200
        log = make_log(
            tmp_path, memory_log_max_entries=DEFAULT_CONFIG["memory_log_max_entries"]
        )
        assert log._max_entries == 200

    def test_rotation_prunes_with_mixed_assets(self, tmp_path):
        """자산군이 섞인 로그에서도 상한 초과 시 가장 오래된 확정 항목부터 정리되는지 검증하는 테스트."""
        log = make_log(tmp_path, memory_log_max_entries=2)
        _resolve_entry(log, "AAPL", "2026-01-01", DECISION_BUY, "Old stock.", asset_type="stock")
        _resolve_entry(log, "BTC-USD", "2026-01-02", DECISION_BUY, "Crypto.", asset_type="crypto")
        _resolve_entry(log, "MSFT", "2026-01-03", DECISION_BUY, "New stock.", asset_type="stock")
        entries = log.load_entries()
        assert len(entries) == 2
        assert [e["ticker"] for e in entries] == ["BTC-USD", "MSFT"]
        # 로테이션 후에도 자산군 태그가 유지된다.
        assert entries[0]["asset_type"] == "crypto"
