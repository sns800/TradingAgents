# =============================================================================
# [테스트 개요]
# scripts/risk_debate_probe.py (리스크 토론 재생성 + PM 게이트 결합 프로브)의
# 순수 로직 테스트. LLM은 모킹하며 다음을 검증한다:
#   - normalize_state가 저장된(편향된) risk_debate_state를 빈 상태로 초기화 (재생성 전제)
#   - run_risk_debate가 실제 노드 + 실제 conditional_logic로 3N+1 발언을 라이브 순서대로 생성
#   - classify_override 밴드 이동 방향 (pm_probe와 동일 계약)
#   - interleave가 (root,ticker) 그룹을 라운드로빈으로 섞음 (원소 보존)
#   - process_sample의 재생성→PM 재판정·폴백·실패 격리 (LLM 모킹, watchdog 비활성)
#   - summarize의 밴드 분포·양방향 override 보고
# =============================================================================
"""risk_debate_probe 결합 로직 단위 테스트 (LLM 모킹)."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tradingagents.agents.schemas import PortfolioDecision, PortfolioRating

# scripts/는 패키지가 아니므로 파일 경로로 직접 로드한다.
_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "risk_debate_probe.py"
_spec = importlib.util.spec_from_file_location("risk_debate_probe", _SCRIPT_PATH)
rdp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rdp)


@pytest.fixture
def state():
    """상태 로그(full_states_log)의 최소 재현 — 실제 키 구조와 동일.

    instrument_context를 직접 넣어 헬퍼가 네트워크 조회 경로로 가지 않게 한다.
    저장된 risk_debate_state(옛 편향 대본)를 일부러 채워, normalize_state가
    이를 버리고 빈 상태로 초기화하는지 검증할 수 있게 한다.
    """
    return {
        "company_of_interest": "AAPL",
        "trade_date": "2025-09-01",
        "instrument_context": "Instrument: AAPL (stock)",
        "market_report": "시장 보고서 본문.",
        "sentiment_report": "감성 보고서 본문.",
        "news_report": "뉴스 보고서 본문.",
        "fundamentals_report": "펀더멘탈 보고서 본문.",
        "investment_plan": "**Recommendation**: Overweight\n\n**Rationale**: ...",
        "trader_investment_decision": "FINAL TRANSACTION PROPOSAL: **BUY**",
        "risk_debate_state": {
            "history": "옛 편향 대본 — 재생성 시 버려져야 함",
            "aggressive_history": "stale-a",
            "conservative_history": "stale-c",
            "neutral_history": "stale-n",
            "count": 99,
            "latest_speaker": "Judge",
        },
    }


class _FakeLLM:
    """토론자용 가짜 LLM — 호출 수를 세고 짧은 발언을 돌려준다."""

    def __init__(self, tag: str = "arg"):
        self.tag = tag
        self.calls = 0

    def invoke(self, prompt):
        self.calls += 1
        return SimpleNamespace(content=f"{self.tag}#{self.calls}")


def _debator_nodes(fake: _FakeLLM) -> dict:
    """실제 토론자 팩토리로 노드 3종을 만든다 (실제 상태 갱신 로직을 그대로 검증)."""
    return {
        "Aggressive Analyst": rdp.create_aggressive_debator(fake),
        "Conservative Analyst": rdp.create_conservative_debator(fake),
        "Neutral Analyst": rdp.create_neutral_debator(fake),
    }


# ---------------------------------------------------------------------------
# 상태 정규화 — 저장된 편향 대본을 버리고 재생성 전제로 초기화
# ---------------------------------------------------------------------------


def test_normalize_state_resets_risk_debate_state(state):
    norm = rdp.normalize_state(state)
    rds = norm["risk_debate_state"]
    assert rds["count"] == 0
    assert rds["history"] == ""
    assert rds["aggressive_history"] == ""
    assert rds["latest_speaker"] == ""
    # 트레이더 키도 라이브 노드용으로 매핑된다.
    assert norm["trader_investment_plan"] == "FINAL TRANSACTION PROPOSAL: **BUY**"
    # 원본은 변형되지 않는다.
    assert state["risk_debate_state"]["count"] == 99


def test_normalize_state_fills_missing_reports():
    norm = rdp.normalize_state({"trader_investment_decision": "x"})
    for k in ("market_report", "sentiment_report", "news_report", "fundamentals_report"):
        assert norm[k] == ""


# ---------------------------------------------------------------------------
# 리스크 토론 재생성 — 실제 노드 + 실제 conditional_logic로 3N+1 발언
# ---------------------------------------------------------------------------


def test_run_risk_debate_produces_3n_plus_1_turns_in_live_order(state):
    fake = _FakeLLM()
    nodes = _debator_nodes(fake)
    cond = rdp.ConditionalLogic(max_risk_discuss_rounds=1)
    norm = rdp.normalize_state(state)

    out_state, turns = rdp.run_risk_debate(norm, nodes, cond)

    # N=1 → 3N+1 = 4 발언, LLM도 정확히 4회 호출.
    assert turns == 4
    assert fake.calls == 4
    rds = out_state["risk_debate_state"]
    assert rds["count"] == 4
    # 라이브 순서: Aggressive → Conservative → Neutral → Aggressive (선발언자 마지막 재반박).
    speakers = [line.split(" Analyst:")[0] for line in rds["history"].strip().splitlines()]
    assert speakers == ["Aggressive", "Conservative", "Neutral", "Aggressive"]
    # 마지막 발언자는 Aggressive, 각 화자 이력이 채워진다.
    assert rds["latest_speaker"] == "Aggressive"
    assert rds["aggressive_history"].count("Aggressive Analyst:") == 2
    assert rds["conservative_history"].count("Conservative Analyst:") == 1
    assert rds["neutral_history"].count("Neutral Analyst:") == 1


def test_run_risk_debate_two_rounds(state):
    fake = _FakeLLM()
    nodes = _debator_nodes(fake)
    cond = rdp.ConditionalLogic(max_risk_discuss_rounds=2)
    _, turns = rdp.run_risk_debate(rdp.normalize_state(state), nodes, cond)
    assert turns == 3 * 2 + 1  # 7


def test_run_risk_debate_uses_fresh_state_each_call(state):
    """재생성은 항상 빈 상태에서 시작 — 입력 state의 옛 대본에 오염되지 않는다."""
    fake = _FakeLLM()
    nodes = _debator_nodes(fake)
    cond = rdp.ConditionalLogic(max_risk_discuss_rounds=1)
    # 정규화하지 않은(옛 대본이 남은) 상태를 넘겨도 재생성은 빈 상태에서 출발.
    dirty = dict(state)
    dirty["trader_investment_plan"] = "x"
    out_state, turns = rdp.run_risk_debate(dirty, nodes, cond)
    assert turns == 4
    assert out_state["risk_debate_state"]["count"] == 4
    assert "옛 편향 대본" not in out_state["risk_debate_state"]["history"]


# ---------------------------------------------------------------------------
# override 분류 (밴드 이동 방향)
# ---------------------------------------------------------------------------


def test_classify_override_directions():
    assert rdp.classify_override("Overweight", "Overweight") == "confirm"
    assert rdp.classify_override("Overweight", "Hold") == "downgrade"
    assert rdp.classify_override("Hold", "Overweight") == "upgrade"
    assert rdp.classify_override("Underweight", "Buy") == "upgrade"


# ---------------------------------------------------------------------------
# 인터리브 — (root,ticker) 라운드로빈, 원소 보존
# ---------------------------------------------------------------------------


def test_interleave_round_robins_groups_and_preserves_all():
    samples = [
        {"root": "backtest", "ticker": "AAPL", "date": "d1"},
        {"root": "backtest", "ticker": "AAPL", "date": "d2"},
        {"root": "backtest", "ticker": "MSFT", "date": "d1"},
        {"root": "backtest_phase3", "ticker": "NVDA", "date": "d1"},
    ]
    out = rdp.interleave(samples)
    # 원소 보존 (개수·집합).
    assert len(out) == 4
    assert {(s["ticker"], s["date"]) for s in out} == {
        ("AAPL", "d1"), ("AAPL", "d2"), ("MSFT", "d1"), ("NVDA", "d1")
    }
    # 첫 3개는 서로 다른 그룹에서 하나씩(라운드로빈) 나온다.
    first3 = [(s["root"], s["ticker"]) for s in out[:3]]
    assert len(set(first3)) == 3


# ---------------------------------------------------------------------------
# process_sample — 재생성 → PM 재판정 (LLM 모킹, watchdog 비활성)
# ---------------------------------------------------------------------------


class _StructuredPM:
    def __init__(self, final: PortfolioRating, action: str):
        self._final = final
        self._action = action

    def invoke(self, prompt):
        return PortfolioDecision(
            rm_proposed_rating=PortfolioRating.OVERWEIGHT,
            override_action=self._action,
            override_rationale="구체적 집중 리스크가 토론에서 해소되지 않음.",
            rating=self._final,
            executive_summary="s",
            investment_thesis="t",
        )


def _sample(state):
    return {"root": "backtest", "ticker": "AAPL", "date": "2025-09-01",
            "state": rdp.normalize_state(state)}


def test_process_sample_regenerates_debate_then_gates(state):
    fake = _FakeLLM()
    nodes = _debator_nodes(fake)
    cond = rdp.ConditionalLogic(max_risk_discuss_rounds=1)
    pm = _StructuredPM(PortfolioRating.HOLD, "downgrade")

    row = rdp.process_sample(_sample(state), nodes, cond, pm, plain_pm_llm=None,
                             combo_timeout=None)

    assert row["rm_rating"] == "Overweight"
    assert row["final_rating"] == "Hold"
    assert row["override"] == "downgrade"
    assert row["debate_turns"] == 4  # 토론이 실제로 재생성됨
    assert fake.calls == 4           # 토론자 LLM 4회
    assert row["override_rationale"].startswith("구체적")


def test_process_sample_records_upgrade_direction(state):
    """상향(양방향 신호)도 올바르게 분류된다."""
    fake = _FakeLLM()
    nodes = _debator_nodes(fake)
    cond = rdp.ConditionalLogic(max_risk_discuss_rounds=1)
    pm = _StructuredPM(PortfolioRating.BUY, "upgrade")
    row = rdp.process_sample(_sample(state), nodes, cond, pm, None, combo_timeout=None)
    assert row["final_rating"] == "Buy"
    assert row["override"] == "upgrade"


def test_process_sample_falls_back_to_freetext(state):
    class _NonePM:
        def invoke(self, prompt):
            return None

    class _PlainPM:
        def invoke(self, prompt):
            return SimpleNamespace(content="근거...\n\nRating: Underweight")

    fake = _FakeLLM()
    nodes = _debator_nodes(fake)
    cond = rdp.ConditionalLogic(max_risk_discuss_rounds=1)
    row = rdp.process_sample(_sample(state), nodes, cond, _NonePM(), _PlainPM(),
                             combo_timeout=None)
    assert row.get("fallback") is True
    assert row["final_rating"] == "Underweight"
    assert row["override"] == "downgrade"


def test_process_sample_isolates_failure(state):
    class _BoomPM:
        def invoke(self, prompt):
            raise RuntimeError("boom")

    fake = _FakeLLM()
    nodes = _debator_nodes(fake)
    cond = rdp.ConditionalLogic(max_risk_discuss_rounds=1)
    row = rdp.process_sample(_sample(state), nodes, cond, _BoomPM(), None,
                             combo_timeout=None)
    assert "error" in row and "boom" in row["error"]
    assert "override" not in row


def test_run_probe_writes_jsonl(tmp_path, state):
    fake = _FakeLLM()
    nodes = _debator_nodes(fake)
    cond = rdp.ConditionalLogic(max_risk_discuss_rounds=1)
    pm = _StructuredPM(PortfolioRating.HOLD, "downgrade")
    out = tmp_path / "results.jsonl"
    rows = rdp.run_probe([_sample(state)], nodes, cond, pm, None, out,
                         combo_timeout=None)
    assert len(rows) == 1
    written = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert written[0]["override"] == "downgrade"


# ---------------------------------------------------------------------------
# 요약 — 양방향 override · 밴드 분포
# ---------------------------------------------------------------------------


def test_summarize_reports_bidirectional_and_bands():
    rows = [
        {"root": "backtest", "ticker": "A", "date": "d", "rm_rating": "Overweight",
         "final_rating": "Hold", "override": "downgrade", "override_rationale": "리스크"},
        {"root": "backtest", "ticker": "B", "date": "d", "rm_rating": "Hold",
         "final_rating": "Overweight", "override": "upgrade", "override_rationale": "과도 보수"},
        {"root": "backtest_phase3", "ticker": "C", "date": "d", "rm_rating": "Hold",
         "final_rating": "Hold", "override": "confirm"},
        {"root": "backtest", "ticker": "D", "date": "d", "error": "X: boom"},
    ]
    md = rdp.summarize(rows)
    assert "실패: 1" in md
    # 양방향: 하향 1, 상향 1.
    assert "downgrade (하향) | 1" in md
    assert "upgrade (상향) | 1" in md
    # 밴드 분포 표가 포함된다.
    assert "강세(Buy+Overweight)" in md
    assert "약세(Underweight+Sell)" in md
