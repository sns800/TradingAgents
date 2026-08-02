# =============================================================================
# [테스트 개요]
# scripts/pm_probe.py (포트폴리오 매니저 리스크 감독 게이트 재판정 프로브)의
# 순수 로직 테스트. LLM 호출은 모킹하며 다음을 검증한다:
#   - 저장 로그 → 라이브 PM 프롬프트 빌더용 상태 정규화 (normalize_state)
#   - RM 등급 대비 밴드 이동 방향 분류 (classify_override)
#   - 다중 로그 루트 로드와 루트 태깅 (load_samples)
#   - run_probe의 재판정·JSONL 기록·폴백 경로 (LLM 모킹)
# =============================================================================
"""pm_probe 재판정 로직 단위 테스트 (LLM 모킹)."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tradingagents.agents.schemas import PortfolioDecision, PortfolioRating

# scripts/는 패키지가 아니므로 파일 경로로 직접 로드한다.
_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "pm_probe.py"
_spec = importlib.util.spec_from_file_location("pm_probe", _SCRIPT_PATH)
pm_probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pm_probe)


@pytest.fixture
def state():
    """상태 로그(full_states_log)의 최소 재현 — 실제 키 구조와 동일."""
    return {
        "company_of_interest": "AAPL",
        "trade_date": "2025-09-01",
        "market_report": "시장 보고서 본문.",
        "sentiment_report": "감성 보고서 본문.",
        "news_report": "뉴스 보고서 본문.",
        "fundamentals_report": "펀더멘탈 보고서 본문.",
        "investment_plan": "**Recommendation**: Overweight\n\n**Rationale**: ...",
        # 저장 로그는 트레이더 산출물을 이 키로 직렬화한다 (trader_investment_plan 아님).
        "trader_investment_decision": "FINAL TRANSACTION PROPOSAL: **BUY**",
        "risk_debate_state": {
            "history": "Aggressive: ... Conservative: ... Neutral: ...",
            "aggressive_history": "a",
            "conservative_history": "c",
            "neutral_history": "n",
            "judge_decision": "",
        },
    }


# ---------------------------------------------------------------------------
# 상태 정규화
# ---------------------------------------------------------------------------


def test_normalize_state_maps_trader_decision_key(state):
    norm = pm_probe.normalize_state(state)
    assert norm["trader_investment_plan"] == "FINAL TRANSACTION PROPOSAL: **BUY**"
    # 원본은 변형되지 않는다.
    assert "trader_investment_plan" not in state


def test_normalize_state_keeps_existing_plan(state):
    state["trader_investment_plan"] = "existing"
    assert pm_probe.normalize_state(state)["trader_investment_plan"] == "existing"


def test_normalized_state_builds_pm_prompt(state):
    """정규화된 상태로 라이브 PM 프롬프트 빌더가 KeyError 없이 동작한다."""
    prompt = pm_probe.build_portfolio_manager_prompt(pm_probe.normalize_state(state))
    assert "RISK-OVERSIGHT gate" in prompt
    # RM 등급이 앵커로 추출·제시된다.
    assert "The Research Manager proposed: Overweight" in prompt


# ---------------------------------------------------------------------------
# override 분류 (밴드 이동 방향)
# ---------------------------------------------------------------------------


def test_classify_override_confirm():
    assert pm_probe.classify_override("Overweight", "Overweight") == "confirm"


def test_classify_override_downgrade_is_toward_sell():
    # Overweight → Hold, Buy → Sell 등 약세 방향은 하향(자본 보호).
    assert pm_probe.classify_override("Overweight", "Hold") == "downgrade"
    assert pm_probe.classify_override("Buy", "Sell") == "downgrade"
    assert pm_probe.classify_override("Hold", "Underweight") == "downgrade"


def test_classify_override_upgrade_is_toward_buy():
    assert pm_probe.classify_override("Hold", "Overweight") == "upgrade"
    assert pm_probe.classify_override("Underweight", "Buy") == "upgrade"


# ---------------------------------------------------------------------------
# 다중 로그 루트 로드
# ---------------------------------------------------------------------------


def _write_log(root: Path, ticker: str, date: str, state: dict):
    state_dir = root / ticker / "TradingAgentsStrategy_logs"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / f"full_states_log_{date}.json").write_text(
        json.dumps(state), encoding="utf-8"
    )


def test_load_samples_across_two_roots_tags_each(tmp_path, state):
    root_a = tmp_path / "backtest"
    root_b = tmp_path / "backtest_phase3"
    _write_log(root_a, "AAPL", "2025-09-01", state)
    _write_log(root_b, "MSFT", "2025-02-03", state)

    samples = pm_probe.load_samples([root_a, root_b])
    assert len(samples) == 2
    by_root = {s["root"]: s for s in samples}
    assert by_root["backtest"]["ticker"] == "AAPL"
    assert by_root["backtest_phase3"]["ticker"] == "MSFT"
    # 로드 시 상태가 정규화되어 프롬프트 빌더가 바로 쓸 수 있다.
    assert "trader_investment_plan" in by_root["backtest"]["state"]


def test_load_samples_missing_root_is_skipped(tmp_path, state):
    root_a = tmp_path / "backtest"
    _write_log(root_a, "AAPL", "2025-09-01", state)
    samples = pm_probe.load_samples([root_a, tmp_path / "does_not_exist"])
    assert len(samples) == 1


# ---------------------------------------------------------------------------
# run_probe — 재판정 · JSONL 기록 (LLM 모킹)
# ---------------------------------------------------------------------------


class _StructuredLLM:
    """지정한 최종 등급의 PortfolioDecision을 돌려주는 가짜 구조화 LLM."""

    def __init__(self, final_rating: PortfolioRating, action: str):
        self._final = final_rating
        self._action = action
        self.calls = 0

    def invoke(self, prompt):
        self.calls += 1
        return PortfolioDecision(
            rm_proposed_rating=PortfolioRating.OVERWEIGHT,
            override_action=self._action,
            override_rationale="집중 리스크가 토론에서 해소되지 않음.",
            rating=self._final,
            executive_summary="s",
            investment_thesis="t",
        )


def test_run_probe_records_override_and_writes_jsonl(tmp_path, state):
    structured = _StructuredLLM(PortfolioRating.HOLD, "downgrade")
    samples = [{"root": "backtest", "ticker": "AAPL", "date": "2025-09-01",
                "state": pm_probe.normalize_state(state)}]
    out = tmp_path / "results.jsonl"
    rows = pm_probe.run_probe(structured, plain_llm=None, samples=samples, out_jsonl=out, workers=1)

    assert len(rows) == 1
    r = rows[0]
    assert r["rm_rating"] == "Overweight"
    assert r["final_rating"] == "Hold"
    # RM Overweight → 최종 Hold 는 하향(자본 보호 방향).
    assert r["override"] == "downgrade"
    assert r["override_rationale"].startswith("집중 리스크")
    # JSONL에도 동일 행이 기록된다.
    written = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert written[0]["override"] == "downgrade"


def test_run_probe_falls_back_to_freetext_when_structured_returns_none(tmp_path, state):
    """구조화 결과가 None이면 자유 텍스트 폴백으로 등급을 파싱한다."""

    class _NoneStructured:
        def invoke(self, prompt):
            return None

    class _PlainLLM:
        def invoke(self, prompt):
            return SimpleNamespace(content="상세 근거...\n\nRating: Underweight")

    samples = [{"root": "backtest", "ticker": "AAPL", "date": "2025-09-01",
                "state": pm_probe.normalize_state(state)}]
    out = tmp_path / "results.jsonl"
    rows = pm_probe.run_probe(_NoneStructured(), _PlainLLM(), samples, out, workers=1)
    r = rows[0]
    assert r.get("fallback") is True
    assert r["final_rating"] == "Underweight"
    assert r["override"] == "downgrade"


def test_run_probe_isolates_failed_samples(tmp_path, state):
    class _BoomLLM:
        def invoke(self, prompt):
            raise RuntimeError("boom")

    samples = [{"root": "backtest", "ticker": "AAPL", "date": "2025-09-01",
                "state": pm_probe.normalize_state(state)}]
    out = tmp_path / "results.jsonl"
    rows = pm_probe.run_probe(_BoomLLM(), plain_llm=None, samples=samples, out_jsonl=out, workers=1)
    assert "error" in rows[0] and "boom" in rows[0]["error"]
    assert "override" not in rows[0]


# ---------------------------------------------------------------------------
# 요약
# ---------------------------------------------------------------------------


def test_summarize_reports_override_rate_and_direction():
    rows = [
        {"root": "backtest", "ticker": "A", "date": "d", "rm_rating": "Overweight",
         "final_rating": "Hold", "override": "downgrade",
         "override_rationale": "리스크"},
        {"root": "backtest", "ticker": "B", "date": "d", "rm_rating": "Hold",
         "final_rating": "Hold", "override": "confirm"},
        {"root": "backtest_phase3", "ticker": "C", "date": "d", "rm_rating": "Hold",
         "final_rating": "Overweight", "override": "upgrade",
         "override_rationale": "과도 보수"},
        {"root": "backtest", "ticker": "D", "date": "d", "error": "X: boom"},
    ]
    md = pm_probe.summarize(rows)
    # override율 = 2/3 (성공 3개 중 downgrade 1 + upgrade 1).
    assert "66.7%" in md
    assert "downgrade (하향, 자본 보호) | 1" in md
    assert "upgrade (상향) | 1" in md
    # 실패 표본은 성공 집계에서 제외되고 별도로 보고된다.
    assert "실패: 1" in md
