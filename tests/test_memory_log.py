# 이 파일은 거래 기억 로그(TradingMemoryLog)를 검증하는 테스트 모음입니다.
# 결정 저장, 결과 확정 후의 지연 회고(deferred reflection), 포트폴리오 매니저(PM)
# 프롬프트 주입, 구식 메모리 코드 제거를 확인합니다.
"""TradingMemoryLog 테스트 — 저장, 지연 회고(deferred reflection), PM 주입, 레거시 제거."""

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
from tradingagents.agents.schemas import PortfolioDecision, PortfolioRating
from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.graph.propagation import Propagator
from tradingagents.graph.reflection import Reflector
from tradingagents.graph.trading_graph import TradingAgentsGraph

_SEP = TradingMemoryLog._SEPARATOR

DECISION_BUY = "Rating: Buy\nEnter at $189-192, 6% portfolio cap."
DECISION_OVERWEIGHT = (
    "Rating: Overweight\n"
    "Executive Summary: Moderate position, await confirmation.\n"
    "Investment Thesis: Strong fundamentals but near-term headwinds."
)
DECISION_SELL = "Rating: Sell\nExit position immediately."
DECISION_NO_RATING = (
    "Executive Summary: Complex situation with multiple competing factors.\n"
    "Investment Thesis: No clear directional signal at this time."
)


# ---------------------------------------------------------------------------
# 공용 헬퍼
# ---------------------------------------------------------------------------

def make_log(tmp_path, filename="trading_memory.md"):
    config = {"memory_log_path": str(tmp_path / filename)}
    return TradingMemoryLog(config)


def _seed_completed(tmp_path, ticker, date, decision_text, reflection_text, filename="trading_memory.md"):
    """API를 거치지 않고 완료된 항목을 파일에 직접 기록하는 헬퍼."""
    entry = (
        f"[{date} | {ticker} | Buy | +1.0% | +0.5% | 5d]\n\n"
        f"DECISION:\n{decision_text}\n\n"
        f"REFLECTION:\n{reflection_text}"
        + _SEP
    )
    with open(tmp_path / filename, "a", encoding="utf-8") as f:
        f.write(entry)


def _resolve_entry(log, ticker, date, decision, reflection="Good call."):
    """결정을 저장한 뒤 API를 통해 즉시 결과를 확정(resolve)하는 헬퍼."""
    log.store_decision(ticker, date, decision)
    log.update_with_outcome(ticker, date, 0.05, 0.02, 5, reflection)


def _price_df(prices):
    """yfinance .history() 출력 형태에 맞춘 최소한의 DataFrame을 만드는 헬퍼."""
    return pd.DataFrame({"Close": prices})


def _make_pm_state(past_context=""):
    """portfolio_manager_node용 최소한의 AgentState dict를 만드는 헬퍼."""
    return {
        "company_of_interest": "NVDA",
        "past_context": past_context,
        "risk_debate_state": {
            "history": "Risk debate history.",
            "aggressive_history": "",
            "conservative_history": "",
            "neutral_history": "",
            "judge_decision": "",
            "current_aggressive_response": "",
            "current_conservative_response": "",
            "current_neutral_response": "",
            "count": 1,
        },
        "market_report": "Market report.",
        "sentiment_report": "Sentiment report.",
        "news_report": "News report.",
        "fundamentals_report": "Fundamentals report.",
        "investment_plan": "Research plan.",
        "trader_investment_plan": "Trader plan.",
    }


def _structured_pm_llm(captured: dict, decision: PortfolioDecision | None = None):
    """with_structured_output 바인딩이 프롬프트를 캡처하고 실제 PortfolioDecision을
    반환하는 MagicMock LLM을 만드는 헬퍼 (render_pm_decision이 동작하도록).
    """
    if decision is None:
        decision = PortfolioDecision(
            rating=PortfolioRating.HOLD,
            executive_summary="Hold the position; await catalyst.",
            investment_thesis="Balanced view; neither side carried the debate.",
        )
    structured = MagicMock()
    structured.invoke.side_effect = lambda prompt: (
        captured.__setitem__("prompt", prompt) or decision
    )
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    return llm


# ---------------------------------------------------------------------------
# 핵심: 저장과 읽기 경로
# ---------------------------------------------------------------------------

class TestTradingMemoryLogCore:
    """기억 로그의 저장·파싱·조회 등 핵심 동작을 검증하는 테스트 묶음."""

    def test_store_creates_file(self, tmp_path):
        """첫 결정 저장 시 로그 파일이 생성되는지 검증하는 테스트."""
        log = make_log(tmp_path)
        assert not (tmp_path / "trading_memory.md").exists()
        log.store_decision("NVDA", "2026-01-10", DECISION_BUY)
        assert (tmp_path / "trading_memory.md").exists()

    def test_store_appends_not_overwrites(self, tmp_path):
        """새 결정이 기존 항목을 덮어쓰지 않고 뒤에 추가되는지 검증하는 테스트."""
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-10", DECISION_BUY)
        log.store_decision("AAPL", "2026-01-11", DECISION_OVERWEIGHT)
        entries = log.load_entries()
        assert len(entries) == 2
        assert entries[0]["ticker"] == "NVDA"
        assert entries[1]["ticker"] == "AAPL"

    def test_store_decision_idempotent(self, tmp_path):
        """같은 (티커, 날짜)로 store_decision을 두 번 호출해도 항목이 하나만 저장되는지 검증하는 테스트 (멱등성)."""
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-10", DECISION_BUY)
        log.store_decision("NVDA", "2026-01-10", DECISION_BUY)
        assert len(log.load_entries()) == 1

    def test_batch_update_resolves_multiple_entries(self, tmp_path):
        """batch_update_with_outcomes가 여러 대기(pending) 항목을 한 번의 쓰기로 확정하는지 검증하는 테스트."""
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-05", DECISION_BUY)
        log.store_decision("NVDA", "2026-01-12", DECISION_SELL)

        updates = [
            {"ticker": "NVDA", "trade_date": "2026-01-05",
             "raw_return": 0.05, "alpha_return": 0.02, "holding_days": 5,
             "reflection": "First correct."},
            {"ticker": "NVDA", "trade_date": "2026-01-12",
             "raw_return": -0.03, "alpha_return": -0.01, "holding_days": 5,
             "reflection": "Second correct."},
        ]
        log.batch_update_with_outcomes(updates)

        entries = log.load_entries()
        assert len(entries) == 2
        assert all(not e["pending"] for e in entries)
        assert entries[0]["reflection"] == "First correct."
        assert entries[1]["reflection"] == "Second correct."

    def test_pending_tag_format(self, tmp_path):
        """대기 중인 항목의 태그 형식이 올바른지 검증하는 테스트."""
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-10", DECISION_BUY)
        text = (tmp_path / "trading_memory.md").read_text(encoding="utf-8")
        assert "[2026-01-10 | NVDA | Buy | pending]" in text

    # 등급(rating) 파싱

    def test_rating_parsed_buy(self, tmp_path):
        """결정 텍스트에서 Buy 등급이 파싱되는지 검증하는 테스트."""
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-10", DECISION_BUY)
        assert log.load_entries()[0]["rating"] == "Buy"

    def test_rating_parsed_overweight(self, tmp_path):
        """Overweight(비중 확대) 등급이 파싱되는지 검증하는 테스트."""
        log = make_log(tmp_path)
        log.store_decision("AAPL", "2026-01-11", DECISION_OVERWEIGHT)
        assert log.load_entries()[0]["rating"] == "Overweight"

    def test_rating_fallback_hold(self, tmp_path):
        """등급 표기가 없으면 기본값인 Hold로 처리되는지 검증하는 테스트."""
        log = make_log(tmp_path)
        log.store_decision("MSFT", "2026-01-12", DECISION_NO_RATING)
        assert log.load_entries()[0]["rating"] == "Hold"

    def test_rating_priority_over_prose(self, tmp_path):
        """본문에 상반된 등급 단어가 먼저 나와도 'Rating: X' 라벨이 우선하는지 검증하는 테스트."""
        decision = (
            "The sell thesis is weak. The hold case is marginal.\n\n"
            "Rating: Buy\n\n"
            "Executive Summary: Strong fundamentals support the position."
        )
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-10", decision)
        assert log.load_entries()[0]["rating"] == "Buy"

    # 구분자(delimiter) 견고성

    def test_decision_with_markdown_separator(self, tmp_path):
        """LLM 결정문에 '---'가 포함되어도 항목이 손상되지 않는지 검증하는 테스트."""
        decision = "Rating: Buy\n\n---\n\nRisk: elevated volatility."
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-10", decision)
        entries = log.load_entries()
        assert len(entries) == 1
        assert "Risk: elevated volatility" in entries[0]["decision"]

    # load_entries

    def test_load_entries_empty_file(self, tmp_path):
        """로그 파일이 없으면 빈 목록을 반환하는지 검증하는 테스트."""
        log = make_log(tmp_path)
        assert log.load_entries() == []

    def test_load_entries_single(self, tmp_path):
        """단일 항목이 모든 필드와 함께 올바르게 파싱되는지 검증하는 테스트."""
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-10", DECISION_BUY)
        entries = log.load_entries()
        assert len(entries) == 1
        e = entries[0]
        assert e["date"] == "2026-01-10"
        assert e["ticker"] == "NVDA"
        assert e["rating"] == "Buy"
        assert e["pending"] is True
        assert e["raw"] is None

    def test_load_entries_multiple(self, tmp_path):
        """여러 항목이 저장 순서대로 로드되는지 검증하는 테스트."""
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-10", DECISION_BUY)
        log.store_decision("AAPL", "2026-01-11", DECISION_OVERWEIGHT)
        log.store_decision("MSFT", "2026-01-12", DECISION_NO_RATING)
        entries = log.load_entries()
        assert len(entries) == 3
        assert [e["ticker"] for e in entries] == ["NVDA", "AAPL", "MSFT"]

    def test_decision_content_preserved(self, tmp_path):
        """결정 본문이 손실 없이 그대로 보존되는지 검증하는 테스트."""
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-10", DECISION_BUY)
        assert log.load_entries()[0]["decision"] == DECISION_BUY.strip()

    # get_pending_entries

    def test_get_pending_returns_pending_only(self, tmp_path):
        """완료된 항목은 제외하고 대기 중인 항목만 반환하는지 검증하는 테스트."""
        log = make_log(tmp_path)
        _seed_completed(tmp_path, "NVDA", "2026-01-05", "Buy NVDA.", "Correct.")
        log.store_decision("NVDA", "2026-01-10", DECISION_BUY)
        pending = log.get_pending_entries()
        assert len(pending) == 1
        assert pending[0]["ticker"] == "NVDA"
        assert pending[0]["date"] == "2026-01-10"

    # get_past_context

    def test_get_past_context_empty(self, tmp_path):
        """로그가 비어 있으면 과거 컨텍스트도 빈 문자열인지 검증하는 테스트."""
        log = make_log(tmp_path)
        assert log.get_past_context("NVDA") == ""

    def test_get_past_context_pending_excluded(self, tmp_path):
        """아직 결과가 없는 대기 항목은 과거 컨텍스트에서 제외되는지 검증하는 테스트."""
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-10", DECISION_BUY)
        assert log.get_past_context("NVDA") == ""

    def test_get_past_context_same_ticker(self, tmp_path):
        """같은 티커의 완료 항목이 과거 분석 섹션에 포함되는지 검증하는 테스트."""
        log = make_log(tmp_path)
        _seed_completed(tmp_path, "NVDA", "2026-01-05", "Buy NVDA — AI capex thesis intact.", "Directionally correct.")
        ctx = log.get_past_context("NVDA")
        assert "Past analyses of NVDA" in ctx
        assert "Buy NVDA" in ctx

    def test_get_past_context_cross_ticker(self, tmp_path):
        """다른 티커의 교훈은 교차 티커(cross-ticker) 섹션에 들어가는지 검증하는 테스트."""
        log = make_log(tmp_path)
        _seed_completed(tmp_path, "AAPL", "2026-01-05", "Buy AAPL — Services growth.", "Correct.")
        ctx = log.get_past_context("NVDA")
        assert "Recent cross-ticker lessons" in ctx
        assert "Past analyses of NVDA" not in ctx

    def test_n_same_limit_respected(self, tmp_path):
        """같은 티커 항목은 최근 n_same개까지만 포함되는지 검증하는 테스트."""
        log = make_log(tmp_path)
        for i in range(6):
            _seed_completed(tmp_path, "NVDA", f"2026-01-{i+1:02d}", f"Buy entry {i}.", "Correct.")
        ctx = log.get_past_context("NVDA", n_same=5)
        assert "Buy entry 0" not in ctx
        assert "Buy entry 5" in ctx

    def test_n_cross_limit_respected(self, tmp_path):
        """교차 티커 항목은 최근 n_cross개까지만 포함되는지 검증하는 테스트."""
        log = make_log(tmp_path)
        for i, ticker in enumerate(["AAPL", "MSFT", "GOOG", "META"]):
            _seed_completed(tmp_path, ticker, f"2026-01-{i+1:02d}", f"Buy {ticker}.", "Correct.")
        ctx = log.get_past_context("NVDA", n_cross=3)
        assert "AAPL" not in ctx
        assert "META" in ctx

    # 설정이 None이면 아무 동작도 하지 않음

    def test_no_log_path_is_noop(self):
        """로그 경로 설정이 없으면 모든 연산이 무해한 no-op이 되는지 검증하는 테스트."""
        log = TradingMemoryLog(config=None)
        log.store_decision("NVDA", "2026-01-10", DECISION_BUY)
        assert log.load_entries() == []
        assert log.get_past_context("NVDA") == ""

    # 회전(rotation): 확정된 항목 수에 대한 선택적 상한

    def test_rotation_disabled_by_default(self, tmp_path):
        """max_entries가 없으면 확정된 항목이 모두 보존되는지 검증하는 테스트."""
        log = make_log(tmp_path)
        for i in range(7):
            _resolve_entry(log, "NVDA", f"2026-01-{i+1:02d}", DECISION_BUY, f"Lesson {i}.")
        assert len(log.load_entries()) == 7

    def test_rotation_prunes_oldest_resolved(self, tmp_path):
        """max_entries를 초과하면 가장 오래된 확정 항목부터 정리(prune)되는지 검증하는 테스트."""
        log = TradingMemoryLog({
            "memory_log_path": str(tmp_path / "trading_memory.md"),
            "memory_log_max_entries": 3,
        })
        # 5개 항목을 확정하면, 회전에 의해 최근 3개만 남아야 합니다.
        for i in range(5):
            _resolve_entry(log, "NVDA", f"2026-01-{i+1:02d}", DECISION_BUY, f"Lesson {i}.")
        entries = log.load_entries()
        assert len(entries) == 3
        # 최신이 아닌 가장 오래된 항목이 삭제되었는지 확인.
        dates = [e["date"] for e in entries]
        assert dates == ["2026-01-03", "2026-01-04", "2026-01-05"]

    def test_rotation_never_prunes_pending(self, tmp_path):
        """대기(미확정) 항목은 상한과 무관하게 절대 삭제되지 않는지 검증하는 테스트."""
        log = TradingMemoryLog({
            "memory_log_path": str(tmp_path / "trading_memory.md"),
            "memory_log_max_entries": 2,
        })
        # 확정 3개 + 대기 2개. 상한=2이면 확정 2개만 남고, 대기 2개는 모두 유지됩니다.
        for i in range(3):
            _resolve_entry(log, "NVDA", f"2026-01-{i+1:02d}", DECISION_BUY, f"Resolved {i}.")
        log.store_decision("NVDA", "2026-02-01", DECISION_BUY)
        log.store_decision("NVDA", "2026-02-02", DECISION_OVERWEIGHT)
        # 항목을 하나 더 확정해 회전을 유발 — 대기 항목은 그대로 남아야 합니다.
        _resolve_entry(log, "NVDA", "2026-01-04", DECISION_BUY, "Resolved 3.")
        entries = log.load_entries()
        pending = [e for e in entries if e["pending"]]
        resolved = [e for e in entries if not e["pending"]]
        assert len(pending) == 2, "pending entries must never be pruned"
        assert len(resolved) == 2, f"expected 2 resolved after rotation, got {len(resolved)}"

    def test_rotation_under_cap_is_noop(self, tmp_path):
        """확정 항목 수가 max_entries 이하면 회전이 일어나지 않는지 검증하는 테스트."""
        log = TradingMemoryLog({
            "memory_log_path": str(tmp_path / "trading_memory.md"),
            "memory_log_max_entries": 10,
        })
        for i in range(3):
            _resolve_entry(log, "NVDA", f"2026-01-{i+1:02d}", DECISION_BUY, f"Lesson {i}.")
        assert len(log.load_entries()) == 3

    # 등급 파싱: 마크다운 굵게 표시와 번호 목록 형식

    def test_rating_parsed_from_bold_markdown(self, tmp_path):
        """**Rating**: Buy — 라벨을 감싼 마크다운 굵게 표시가 파싱을 막지 않는지 검증하는 테스트."""
        decision = "**Rating**: Buy\nEnter at $190."
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-10", decision)
        assert log.load_entries()[0]["rating"] == "Buy"

    def test_rating_parsed_from_bold_value(self, tmp_path):
        """Rating: **Sell** — 값을 감싼 마크다운 굵게 표시가 파싱을 막지 않는지 검증하는 테스트."""
        decision = "Rating: **Sell**\nExit immediately."
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-10", decision)
        assert log.load_entries()[0]["rating"] == "Sell"

    def test_rating_label_wins_over_prose_with_markdown(self, tmp_path):
        """본문에 상충하는 등급 단어가 있어도 Rating: **Sell** 라벨이 우선하는지 검증하는 테스트."""
        decision = (
            "The buy thesis is weakened by guidance.\n"
            "Rating: **Sell**\n"
            "Exit before earnings."
        )
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-10", decision)
        assert log.load_entries()[0]["rating"] == "Sell"

    def test_rating_parsed_from_numbered_list(self, tmp_path):
        """1. Rating: Buy — 번호 목록 접두사가 파싱을 막지 않는지 검증하는 테스트."""
        decision = "1. Rating: Buy\nEnter at $190."
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-10", decision)
        assert log.load_entries()[0]["rating"] == "Buy"


# ---------------------------------------------------------------------------
# 지연 회고(deferred reflection): update_with_outcome, Reflector, _fetch_returns
# ---------------------------------------------------------------------------

class TestDeferredReflection:
    """실제 수익률이 확정된 뒤 항목을 갱신하는 지연 회고 흐름을 검증하는 테스트 묶음."""

    # update_with_outcome

    def test_update_replaces_pending_tag(self, tmp_path):
        """결과 갱신 시 pending 태그가 수익률 정보로 교체되는지 검증하는 테스트."""
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-10", DECISION_BUY)
        log.update_with_outcome("NVDA", "2026-01-10", 0.042, 0.021, 5, "Momentum confirmed.")
        text = (tmp_path / "trading_memory.md").read_text(encoding="utf-8")
        assert "[2026-01-10 | NVDA | Buy | pending]" not in text
        assert "+4.2%" in text
        assert "+2.1%" in text
        assert "5d" in text

    def test_update_appends_reflection(self, tmp_path):
        """결과 갱신 시 회고(reflection) 텍스트가 항목에 추가되는지 검증하는 테스트."""
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-10", DECISION_BUY)
        log.update_with_outcome("NVDA", "2026-01-10", 0.042, 0.021, 5, "Momentum confirmed.")
        entries = log.load_entries()
        assert len(entries) == 1
        e = entries[0]
        assert e["pending"] is False
        assert e["reflection"] == "Momentum confirmed."
        assert e["decision"] == DECISION_BUY.strip()

    def test_update_preserves_other_entries(self, tmp_path):
        """일치하는 항목만 수정되고 나머지 항목들은 그대로 유지되는지 검증하는 테스트."""
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-10", DECISION_BUY)
        log.store_decision("AAPL", "2026-01-11", "Rating: Hold\nHold AAPL.")
        log.store_decision("MSFT", "2026-01-12", DECISION_SELL)
        log.update_with_outcome("AAPL", "2026-01-11", 0.01, -0.01, 5, "Neutral result.")
        entries = log.load_entries()
        assert len(entries) == 3
        nvda, aapl, msft = entries
        assert nvda["ticker"] == "NVDA" and nvda["pending"] is True
        assert aapl["ticker"] == "AAPL" and aapl["pending"] is False
        assert aapl["reflection"] == "Neutral result."
        assert msft["ticker"] == "MSFT" and msft["pending"] is True

    def test_update_atomic_write(self, tmp_path):
        """기존에 남아 있던 .tmp 파일이 덮어써지고 로그가 올바르게 갱신되는지 검증하는 테스트 (원자적 쓰기)."""
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-10", DECISION_BUY)
        stale_tmp = tmp_path / "trading_memory.tmp"
        stale_tmp.write_text("GARBAGE CONTENT — should be overwritten", encoding="utf-8")
        log.update_with_outcome("NVDA", "2026-01-10", 0.042, 0.021, 5, "Correct.")
        assert not stale_tmp.exists()
        entries = log.load_entries()
        assert len(entries) == 1
        assert entries[0]["reflection"] == "Correct."
        assert entries[0]["pending"] is False

    def test_update_noop_when_no_log_path(self):
        """로그 경로가 없으면 결과 갱신도 오류 없이 무시되는지 검증하는 테스트."""
        log = TradingMemoryLog(config=None)
        log.update_with_outcome("NVDA", "2026-01-10", 0.05, 0.02, 5, "Reflection")

    def test_formatting_roundtrip_after_update(self, tmp_path):
        """갱신 후에도 모든 필드가 온전하고 태그와 DECISION 사이 빈 줄이 보존되는지 검증하는 테스트."""
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-10", DECISION_BUY)
        log.update_with_outcome("NVDA", "2026-01-10", 0.042, 0.021, 5, "Momentum confirmed.")
        entries = log.load_entries()
        assert len(entries) == 1
        e = entries[0]
        assert e["pending"] is False
        assert e["decision"] == DECISION_BUY.strip()
        assert e["reflection"] == "Momentum confirmed."
        assert e["raw"] == "+4.2%"
        assert e["alpha"] == "+2.1%"
        assert e["holding"] == "5d"
        raw_text = (tmp_path / "trading_memory.md").read_text(encoding="utf-8")
        assert (
            "[2026-01-10 | NVDA | Buy | +4.2% | +2.1% | 5d]\n\nASSET: stock\n\nDECISION:"
            in raw_text
        )

    # Reflector.reflect_on_final_decision

    def test_reflect_on_final_decision_returns_llm_output(self):
        """Reflector가 LLM의 회고 출력을 그대로 반환하는지 검증하는 테스트."""
        mock_llm = MagicMock()
        mock_llm.invoke.return_value.content = "Directionally correct. Thesis confirmed."
        reflector = Reflector(mock_llm)
        result = reflector.reflect_on_final_decision(
            final_decision=DECISION_BUY, raw_return=0.042, alpha_return=0.021
        )
        assert result == "Directionally correct. Thesis confirmed."
        mock_llm.invoke.assert_called_once()

    def test_reflect_on_final_decision_includes_returns_in_prompt(self):
        """LLM에 보내는 사용자 메시지에 수익률 수치가 포함되는지 검증하는 테스트."""
        mock_llm = MagicMock()
        mock_llm.invoke.return_value.content = "Incorrect call."
        reflector = Reflector(mock_llm)
        reflector.reflect_on_final_decision(
            final_decision=DECISION_SELL, raw_return=-0.08, alpha_return=-0.05
        )
        messages = mock_llm.invoke.call_args[0][0]
        human_content = next(content for role, content in messages if role == "human")
        assert "-8.0%" in human_content
        assert "-5.0%" in human_content
        assert "Exit position immediately." in human_content

    # TradingAgentsGraph._fetch_returns

    def test_fetch_returns_valid_ticker(self):
        """정상 티커에 대해 수익률·알파(alpha)·보유일이 계산되는지 검증하는 테스트."""
        stock_prices = [100.0, 102.0, 104.0, 103.0, 105.0, 106.0]
        spy_prices   = [400.0, 402.0, 404.0, 403.0, 405.0, 406.0]
        mock_graph = MagicMock(spec=TradingAgentsGraph)
        with patch("yfinance.Ticker") as mock_ticker_cls:
            def _make_ticker(sym):
                m = MagicMock()
                m.history.return_value = _price_df(spy_prices if sym == "SPY" else stock_prices)
                return m
            mock_ticker_cls.side_effect = _make_ticker
            raw, alpha, days = TradingAgentsGraph._fetch_returns(mock_graph, "NVDA", "2026-01-05")
        assert raw is not None and alpha is not None and days is not None
        assert isinstance(raw, float) and isinstance(alpha, float) and isinstance(days, int)
        assert days == 5

    def test_fetch_returns_too_recent(self):
        """데이터가 1개뿐이면 크래시 없이 (None, None, None)을 반환하는지 검증하는 테스트."""
        mock_graph = MagicMock(spec=TradingAgentsGraph)
        with patch("yfinance.Ticker") as mock_ticker_cls:
            m = MagicMock()
            m.history.return_value = _price_df([100.0])
            mock_ticker_cls.return_value = m
            raw, alpha, days = TradingAgentsGraph._fetch_returns(mock_graph, "NVDA", "2026-04-19")
        assert raw is None and alpha is None and days is None

    def test_fetch_returns_delisted(self):
        """상장 폐지 등으로 빈 DataFrame이면 크래시 없이 (None, None, None)을 반환하는지 검증하는 테스트."""
        mock_graph = MagicMock(spec=TradingAgentsGraph)
        with patch("yfinance.Ticker") as mock_ticker_cls:
            m = MagicMock()
            m.history.return_value = pd.DataFrame({"Close": []})
            mock_ticker_cls.return_value = m
            raw, alpha, days = TradingAgentsGraph._fetch_returns(mock_graph, "XXXXXFAKE", "2026-01-10")
        assert raw is None and alpha is None and days is None

    def test_fetch_returns_spy_shorter_than_stock(self):
        """벤치마크(SPY)의 거래일이 보유 기간보다 적으면 부분 수익률로 조기
        확정하지 않고 (None, None, None)을 반환해 pending을 유지하는지 검증하는 테스트."""
        stock_prices = [100.0, 102.0, 104.0, 103.0, 105.0, 106.0]
        spy_prices   = [400.0, 402.0, 403.0]
        mock_graph = MagicMock(spec=TradingAgentsGraph)
        with patch("yfinance.Ticker") as mock_ticker_cls:
            def _make_ticker(sym):
                m = MagicMock()
                m.history.return_value = _price_df(spy_prices if sym == "SPY" else stock_prices)
                return m
            mock_ticker_cls.side_effect = _make_ticker
            raw, alpha, days = TradingAgentsGraph._fetch_returns(mock_graph, "NVDA", "2026-01-05")
        assert raw is None and alpha is None and days is None

    # TradingAgentsGraph._resolve_benchmark — 알파 계산용 지수를 선택

    def test_resolve_benchmark_explicit_override(self):
        """config['benchmark_ticker']가 지정되면 모든 티커에 대해 우선하는지 검증하는 테스트."""
        mock_graph = MagicMock(spec=TradingAgentsGraph)
        mock_graph.config = {
            "benchmark_ticker": "QQQ",
            "benchmark_map": {"": "SPY", ".T": "^N225"},
        }
        assert TradingAgentsGraph._resolve_benchmark(mock_graph, "7203.T") == "QQQ"
        assert TradingAgentsGraph._resolve_benchmark(mock_graph, "NVDA") == "QQQ"

    def test_resolve_benchmark_suffix_map(self):
        """알려진 거래소 접미사가 해당 지역 지수로 매핑되는지 검증하는 테스트."""
        mock_graph = MagicMock(spec=TradingAgentsGraph)
        mock_graph.config = {
            "benchmark_ticker": None,
            "benchmark_map": {
                ".T": "^N225", ".HK": "^HSI", ".NS": "^NSEI",
                ".L": "^FTSE", ".TO": "^GSPTSE", ".AX": "^AXJO",
                ".BO": "^BSESN", "": "SPY",
            },
        }
        assert TradingAgentsGraph._resolve_benchmark(mock_graph, "7203.T") == "^N225"
        assert TradingAgentsGraph._resolve_benchmark(mock_graph, "0700.HK") == "^HSI"
        assert TradingAgentsGraph._resolve_benchmark(mock_graph, "RELIANCE.NS") == "^NSEI"
        assert TradingAgentsGraph._resolve_benchmark(mock_graph, "AZN.L") == "^FTSE"

    def test_resolve_benchmark_china_a_shares(self):
        """중국 A주 티커가 해당 거래소 종합지수로 매핑되는지 검증하는 테스트
        (A주 지원은 실제 기본 benchmark_map에 의존하므로 그것을 사용)."""
        from tradingagents.default_config import DEFAULT_CONFIG
        mock_graph = MagicMock(spec=TradingAgentsGraph)
        mock_graph.config = {"benchmark_ticker": None,
                             "benchmark_map": DEFAULT_CONFIG["benchmark_map"]}
        assert TradingAgentsGraph._resolve_benchmark(mock_graph, "600519.SS") == "000001.SS"
        assert TradingAgentsGraph._resolve_benchmark(mock_graph, "000001.SZ") == "399001.SZ"

    def test_resolve_benchmark_us_ticker_defaults_to_spy(self):
        """접미사 없는 미국 티커는 빈 접미사 항목(SPY)을 사용하는지 검증하는 테스트."""
        mock_graph = MagicMock(spec=TradingAgentsGraph)
        mock_graph.config = {
            "benchmark_ticker": None,
            "benchmark_map": {"": "SPY", ".T": "^N225"},
        }
        assert TradingAgentsGraph._resolve_benchmark(mock_graph, "NVDA") == "SPY"
        assert TradingAgentsGraph._resolve_benchmark(mock_graph, "AAPL") == "SPY"

    def test_resolve_benchmark_unknown_suffix_falls_back(self):
        """모르는 접미사(BRK.B, FAKE.XX)는 SPY로 대체되는지 검증하는 테스트."""
        mock_graph = MagicMock(spec=TradingAgentsGraph)
        mock_graph.config = {
            "benchmark_ticker": None,
            "benchmark_map": {"": "SPY", ".T": "^N225"},
        }
        assert TradingAgentsGraph._resolve_benchmark(mock_graph, "FAKE.XX") == "SPY"
        assert TradingAgentsGraph._resolve_benchmark(mock_graph, "BRK.B") == "SPY"

    def test_resolve_benchmark_case_insensitive(self):
        """접미사 매칭이 대소문자를 구분하지 않아 7203.t도 7203.T처럼 해석되는지 검증하는 테스트."""
        mock_graph = MagicMock(spec=TradingAgentsGraph)
        mock_graph.config = {
            "benchmark_ticker": None,
            "benchmark_map": {".T": "^N225", "": "SPY"},
        }
        assert TradingAgentsGraph._resolve_benchmark(mock_graph, "7203.t") == "^N225"

    def test_reflector_includes_benchmark_in_label(self):
        """프롬프트 라벨에 하드코딩된 'SPY' 대신 benchmark_name이 나타나는지 검증하는 테스트."""
        mock_llm = MagicMock()
        mock_llm.invoke.return_value.content = "Directionally correct."
        reflector = Reflector(mock_llm)
        reflector.reflect_on_final_decision(
            final_decision=DECISION_BUY,
            raw_return=0.05,
            alpha_return=0.02,
            benchmark_name="^N225",
        )
        messages = mock_llm.invoke.call_args[0][0]
        human_content = next(content for role, content in messages if role == "human")
        assert "Alpha vs ^N225:" in human_content
        assert "Alpha vs SPY:" not in human_content

    def test_reflector_defaults_to_spy_for_unupdated_callers(self):
        """benchmark_name을 넘기지 않는 기존 호출자에게는 SPY 라벨이 유지되는지 검증하는 테스트."""
        mock_llm = MagicMock()
        mock_llm.invoke.return_value.content = "ok"
        reflector = Reflector(mock_llm)
        reflector.reflect_on_final_decision(
            final_decision=DECISION_BUY,
            raw_return=0.05,
            alpha_return=0.02,
        )
        messages = mock_llm.invoke.call_args[0][0]
        human_content = next(content for role, content in messages if role == "human")
        assert "Alpha vs SPY:" in human_content

    # TradingAgentsGraph._resolve_pending_entries

    def test_resolve_skips_other_tickers(self, tmp_path):
        """resolve_all_pending_on_run=False(기존 동작)이면 NVDA 실행에서 대기
        중인 AAPL 항목이 확정되지 않는지 검증하는 테스트. (기본값 True의
        일괄 해소 동작은 test_pending_batch_resolution.py에서 검증한다.)"""
        log = make_log(tmp_path)
        log.store_decision("AAPL", "2026-01-10", DECISION_BUY)
        mock_graph = MagicMock(spec=TradingAgentsGraph)
        mock_graph.memory_log = log
        mock_graph.config = {"holding_days": 5, "resolve_all_pending_on_run": False}
        mock_graph._fetch_returns = MagicMock(return_value=(0.05, 0.02, 5))
        TradingAgentsGraph._resolve_pending_entries(mock_graph, "NVDA")
        mock_graph._fetch_returns.assert_not_called()
        assert len(log.get_pending_entries()) == 1

    def test_resolve_marks_entry_completed(self, tmp_path):
        """확정 후 대기 목록이 비고 항목에 REFLECTION이 채워지는지 검증하는 테스트."""
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-05", DECISION_BUY)
        mock_reflector = MagicMock()
        mock_reflector.reflect_on_final_decision.return_value = "Momentum confirmed."
        mock_graph = MagicMock(spec=TradingAgentsGraph)
        mock_graph.memory_log = log
        mock_graph.reflector = mock_reflector
        mock_graph.config = {"holding_days": 5}
        mock_graph._fetch_returns = MagicMock(return_value=(0.05, 0.02, 5))
        TradingAgentsGraph._resolve_pending_entries(mock_graph, "NVDA")
        assert log.get_pending_entries() == []
        entries = log.load_entries()
        assert len(entries) == 1
        assert entries[0]["pending"] is False
        assert entries[0]["reflection"] == "Momentum confirmed."
        assert "+5.0%" in entries[0]["raw"]
        assert "+2.0%" in entries[0]["alpha"]


# ---------------------------------------------------------------------------
# 포트폴리오 매니저(PM) 주입: 상태와 프롬프트의 past_context
# ---------------------------------------------------------------------------

class TestPortfolioManagerInjection:
    """과거 교훈(past_context)이 상태와 PM 프롬프트에 주입되는지 검증하는 테스트 묶음."""

    # 초기 상태의 past_context

    def test_past_context_in_initial_state(self):
        """전달한 past_context가 초기 상태에 담기는지 검증하는 테스트."""
        propagator = Propagator()
        state = propagator.create_initial_state("NVDA", "2026-01-10", past_context="some context")
        assert "past_context" in state
        assert state["past_context"] == "some context"

    def test_past_context_defaults_to_empty(self):
        """past_context를 넘기지 않으면 빈 문자열이 기본값인지 검증하는 테스트."""
        propagator = Propagator()
        state = propagator.create_initial_state("NVDA", "2026-01-10")
        assert state["past_context"] == ""

    # PM 프롬프트

    def test_pm_prompt_includes_past_context(self):
        """past_context가 있으면 PM 프롬프트에 과거 교훈 섹션이 포함되는지 검증하는 테스트."""
        captured = {}
        llm = _structured_pm_llm(captured)
        pm_node = create_portfolio_manager(llm)
        state = _make_pm_state(past_context="[2026-01-05 | NVDA | Buy | +5.0% | +2.0% | 5d]\nGreat call.")
        pm_node(state)
        assert "Lessons from prior decisions and outcomes" in captured["prompt"]
        assert "Great call." in captured["prompt"]

    def test_pm_no_past_context_no_section(self):
        """past_context가 비어 있으면 PM 프롬프트에서 교훈 섹션이 완전히 생략되는지 검증하는 테스트."""
        captured = {}
        llm = _structured_pm_llm(captured)
        pm_node = create_portfolio_manager(llm)
        state = _make_pm_state(past_context="")
        pm_node(state)
        assert "Lessons from prior decisions" not in captured["prompt"]

    def test_pm_returns_rendered_markdown_with_rating(self):
        """구조화된 PortfolioDecision이 마크다운으로 렌더링되어, 이후 소비자
        (기억 로그, 신호 처리기, CLI 표시)가 추가 LLM 호출 없이 파싱할 수
        있는지 검증하는 테스트."""
        captured = {}
        decision = PortfolioDecision(
            rating=PortfolioRating.OVERWEIGHT,
            executive_summary="Build position gradually over the next two weeks.",
            investment_thesis="AI capex cycle remains intact; institutional flows constructive.",
            price_target=215.0,
            time_horizon="3-6 months",
        )
        llm = _structured_pm_llm(captured, decision)
        pm_node = create_portfolio_manager(llm)
        result = pm_node(_make_pm_state())
        md = result["final_trade_decision"]
        assert "**Rating**: Overweight" in md
        assert "**Executive Summary**: Build position gradually" in md
        assert "**Investment Thesis**: AI capex cycle" in md
        assert "**Price Target**: 215.0" in md
        assert "**Time Horizon**: 3-6 months" in md

    def test_pm_falls_back_to_freetext_when_structured_unavailable(self):
        """제공자가 with_structured_output을 지원하지 않으면 일반 invoke로
        대체해 모델이 생성한 텍스트를 그대로 반환하여, 파이프라인이 절대
        멈추지 않는지 검증하는 테스트."""
        plain_response = "**Rating**: Sell\n\nExit ahead of guidance."
        llm = MagicMock()
        llm.with_structured_output.side_effect = NotImplementedError("provider unsupported")
        llm.invoke.return_value = MagicMock(content=plain_response)
        pm_node = create_portfolio_manager(llm)
        result = pm_node(_make_pm_state())
        assert result["final_trade_decision"] == plain_response

    # get_past_context의 정렬과 개수 제한

    def test_same_ticker_prioritised(self, tmp_path):
        """같은 티커 항목과 교차 티커 항목이 각자의 섹션에 배치되는지 검증하는 테스트."""
        log = make_log(tmp_path)
        _resolve_entry(log, "NVDA", "2026-01-05", DECISION_BUY, "Momentum confirmed.")
        _resolve_entry(log, "AAPL", "2026-01-06", DECISION_SELL, "Overvalued.")
        result = log.get_past_context("NVDA")
        assert "Past analyses of NVDA" in result
        assert "Recent cross-ticker lessons" in result
        same_block, cross_block = result.split("Recent cross-ticker lessons")
        assert "NVDA" in same_block
        assert "AAPL" in cross_block

    def test_cross_ticker_reflection_only(self, tmp_path):
        """교차 티커 항목은 전체 DECISION이 아닌 REFLECTION 텍스트만 노출되는지 검증하는 테스트."""
        log = make_log(tmp_path)
        _resolve_entry(log, "AAPL", "2026-01-06", DECISION_SELL, "Overvalued correction.")
        result = log.get_past_context("NVDA")
        assert "Overvalued correction." in result
        assert "Exit position immediately." not in result

    def test_n_same_limit_respected(self, tmp_path):
        """같은 티커의 완료 항목이 5개를 넘으면 5개만 주입되는지 검증하는 테스트."""
        log = make_log(tmp_path)
        for i in range(7):
            _resolve_entry(log, "NVDA", f"2026-01-{i+1:02d}", DECISION_BUY, f"Lesson {i}.")
        result = log.get_past_context("NVDA", n_same=5)
        lessons_present = sum(1 for i in range(7) if f"Lesson {i}." in result)
        assert lessons_present == 5

    def test_n_cross_limit_respected(self, tmp_path):
        """교차 티커의 완료 항목이 3개를 넘으면 3개만 주입되는지 검증하는 테스트."""
        log = make_log(tmp_path)
        tickers = ["AAPL", "MSFT", "TSLA", "AMZN", "GOOG"]
        for i, ticker in enumerate(tickers):
            _resolve_entry(log, ticker, f"2026-01-{i+1:02d}", DECISION_BUY, f"{ticker} lesson.")
        result = log.get_past_context("NVDA", n_cross=3)
        cross_count = sum(result.count(f"{t} lesson.") for t in tickers)
        assert cross_count == 3

    # A→B→C 전체 통합 사이클

    def test_full_cycle_store_resolve_inject(self, tmp_path):
        """대기 저장 → 결과 확정 → PM용 past_context 생성까지 전체 사이클을 검증하는 테스트."""
        log = make_log(tmp_path)
        log.store_decision("NVDA", "2026-01-05", DECISION_BUY)
        assert len(log.get_pending_entries()) == 1
        assert log.get_past_context("NVDA") == ""
        log.update_with_outcome("NVDA", "2026-01-05", 0.05, 0.02, 5, "Correct call.")
        assert log.get_pending_entries() == []
        past_ctx = log.get_past_context("NVDA")
        assert past_ctx != ""
        assert "NVDA" in past_ctx
        assert "Correct call." in past_ctx
        assert "DECISION:" in past_ctx
        assert "REFLECTION:" in past_ctx


# ---------------------------------------------------------------------------
# 레거시 제거: BM25 / FinancialSituationMemory가 완전히 삭제되었는지 확인
# ---------------------------------------------------------------------------

class TestLegacyRemoval:
    """구식 메모리 구현이 코드베이스에서 완전히 제거되었는지 검증하는 테스트 묶음."""

    def test_financial_situation_memory_removed(self):
        """memory 모듈에서 FinancialSituationMemory를 임포트할 수 없어야 함을 검증하는 테스트."""
        import tradingagents.agents.utils.memory as m
        assert not hasattr(m, "FinancialSituationMemory")

    def test_bm25_not_imported(self):
        """memory 모듈 네임스페이스에 rank_bm25가 없어야 함을 검증하는 테스트."""
        import tradingagents.agents.utils.memory as m
        assert not hasattr(m, "BM25Okapi")

    def test_reflect_and_remember_removed(self):
        """TradingAgentsGraph가 reflect_and_remember를 더 이상 노출하지 않는지 검증하는 테스트."""
        assert not hasattr(TradingAgentsGraph, "reflect_and_remember")

    def test_portfolio_manager_no_memory_param(self):
        """create_portfolio_manager는 llm만 받으며 memory=를 넘기면 TypeError가 나는지 검증하는 테스트."""
        mock_llm = MagicMock()
        create_portfolio_manager(mock_llm)
        with pytest.raises(TypeError):
            create_portfolio_manager(mock_llm, memory=MagicMock())

    def test_full_pipeline_no_regression(self, tmp_path):
        """재설계 이후에도 propagate()가 완료되고 결정이 저장되는지 검증하는 테스트."""
        import functools

        fake_state = {
            "final_trade_decision": "Rating: Buy\nBuy NVDA.",
            "company_of_interest": "NVDA",
            "trade_date": "2026-01-10",
            "market_report": "",
            "sentiment_report": "",
            "news_report": "",
            "fundamentals_report": "",
            "investment_debate_state": {
                "bull_history": "", "bear_history": "", "history": "",
                "current_response": "", "judge_decision": "",
            },
            "investment_plan": "",
            "trader_investment_plan": "",
            "risk_debate_state": {
                "aggressive_history": "", "conservative_history": "",
                "neutral_history": "", "history": "", "judge_decision": "",
                "current_aggressive_response": "", "current_conservative_response": "",
                "current_neutral_response": "", "count": 1, "latest_speaker": "",
            },
        }
        mock_graph = MagicMock()
        mock_graph.memory_log = TradingMemoryLog({"memory_log_path": str(tmp_path / "mem.md")})
        mock_graph.log_states_dict = {}
        mock_graph.debug = False
        mock_graph.config = {"results_dir": str(tmp_path)}
        mock_graph.graph.invoke.return_value = fake_state
        mock_graph.propagator.create_initial_state.return_value = fake_state
        mock_graph.propagator.get_graph_args.return_value = {}
        mock_graph.signal_processor.process_signal.return_value = "Buy"
        # 실제 _run_graph를 바인딩하여 propagate의 self._run_graph 호출이
        # 자동 MagicMock 대신 실제 쓰기 경로를 실행하게 합니다.
        mock_graph._run_graph = functools.partial(
            TradingAgentsGraph._run_graph, mock_graph
        )
        TradingAgentsGraph.propagate(mock_graph, "NVDA", "2026-01-10")
        entries = mock_graph.memory_log.load_entries()
        assert len(entries) == 1
        assert entries[0]["ticker"] == "NVDA"
        assert entries[0]["pending"] is True
