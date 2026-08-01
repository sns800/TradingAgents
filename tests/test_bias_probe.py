# =============================================================================
# [테스트 개요]
# scripts/bias_probe.py (강세 편향 분리 검증 프로브)의 순수 로직 테스트.
# LLM 호출은 전부 모킹하며, 다음만 검증한다:
#   - 토론 이력 블록 분리·순서 반전 (order-swap)
#   - 프롬프트 재구성 (control / no-anti-hold / score-first)
#   - bull/bear 라벨 중립화 (neutral-label)
#   - 점수 JSON 추출과 결정론적 등급 변환 (score-first)
#   - run_probe의 표본별 인터리브 실행과 JSONL 기록
# =============================================================================
"""bias_probe 프롬프트 재구성·변환 로직 단위 테스트 (LLM 모킹)."""

import importlib.util
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

# scripts/는 패키지가 아니므로 파일 경로로 직접 로드한다.
_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "bias_probe.py"
_spec = importlib.util.spec_from_file_location("bias_probe", _SCRIPT_PATH)
bias_probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bias_probe)


HISTORY = (
    "\nBull Analyst: 개시 강세 주장입니다. AAPL 상승 근거."
    "\nBear Analyst: 반박 약세 주장입니다. 밸류에이션 부담."
    "\nBull Analyst: 재반박 강세 주장입니다. bullish 모멘텀 유지."
)


@pytest.fixture
def state():
    """상태 로그(full_states_log)의 최소 재현 — 실제 키 구조와 동일."""
    return {
        "company_of_interest": "AAPL",
        "trade_date": "2025-09-01",
        "market_report": "시장 보고서: RSI 65.96, bullish MACD 신호.",
        "sentiment_report": "감성 보고서: Bearish 40%.",
        "news_report": "뉴스 보고서 본문.",
        "fundamentals_report": "펀더멘탈 보고서 본문.",
        "investment_debate_state": {
            "history": HISTORY,
            "bull_history": "",
            "bear_history": "",
            "current_response": "",
            "judge_decision": "",
        },
    }


# ---------------------------------------------------------------------------
# 토론 블록 분리 · 순서 반전
# ---------------------------------------------------------------------------


def test_split_debate_blocks_finds_all_statements():
    blocks = bias_probe.split_debate_blocks(HISTORY)
    assert len(blocks) == 3
    assert blocks[0].startswith("Bull Analyst: 개시")
    assert blocks[1].startswith("Bear Analyst: 반박")
    assert blocks[2].startswith("Bull Analyst: 재반박")


def test_swap_debate_order_reverses_block_order_only():
    swapped = bias_probe.swap_debate_order(HISTORY)
    blocks = bias_probe.split_debate_blocks(swapped)
    # 순서만 반전: [개시 Bull, Bear, 재반박 Bull] → [재반박 Bull, Bear, 개시 Bull]
    assert blocks[0].startswith("Bull Analyst: 재반박")
    assert blocks[1].startswith("Bear Analyst: 반박")
    assert blocks[2].startswith("Bull Analyst: 개시")
    # 블록 내용 자체는 손실 없이 보존된다.
    assert sorted(blocks) == sorted(bias_probe.split_debate_blocks(HISTORY))


def test_swap_debate_order_single_block_is_noop():
    single = "\nBull Analyst: 혼자 발언."
    assert bias_probe.swap_debate_order(single) == single


# ---------------------------------------------------------------------------
# 프롬프트 재구성
# ---------------------------------------------------------------------------


def test_control_prompt_contains_all_sections(state):
    prompt = bias_probe.build_prompt(state, "control")
    # 원본 research_manager.py의 구성 요소가 모두 포함된다.
    assert bias_probe.RM_PREAMBLE in prompt
    assert "**Rating Scale**" in prompt
    assert bias_probe.ANTI_HOLD_SENTENCE in prompt          # anti-hold 문구 (a)-1
    assert bias_probe.SCHEMA_ANTI_HOLD_SENTENCE in prompt   # anti-hold 문구 (a)-2
    assert "**Evaluation Rubric**" in prompt
    assert state["market_report"] in prompt
    assert state["fundamentals_report"] in prompt
    assert "**Debate History:**" in prompt
    assert "개시 강세 주장입니다" in prompt
    # 등급 파서가 인식할 마지막 등급 줄 지시가 있다.
    assert "Rating: <X>" in prompt


def test_no_anti_hold_removes_only_anti_hold_sentences(state):
    control = bias_probe.build_prompt(state, "control")
    probe = bias_probe.build_prompt(state, "no-anti-hold")
    assert bias_probe.ANTI_HOLD_SENTENCE not in probe
    assert bias_probe.SCHEMA_ANTI_HOLD_SENTENCE not in probe
    # anti-hold 문구 2곳을 제외하면 control과 동일해야 한다.
    reconstructed = control.replace(f"\n\n{bias_probe.ANTI_HOLD_SENTENCE}", "").replace(
        bias_probe.SCHEMA_ANTI_HOLD_SENTENCE, ""
    )
    assert probe == reconstructed


def test_order_swap_prompt_swaps_history_only(state):
    control = bias_probe.build_prompt(state, "control")
    swapped = bias_probe.build_prompt(state, "order-swap")
    # 토론 이력에서 재반박(원래 마지막) 발언이 개시 발언보다 앞에 온다.
    assert swapped.index("재반박 강세") < swapped.index("반박 약세") < swapped.index("개시 강세")
    assert control.index("개시 강세") < control.index("반박 약세") < control.index("재반박 강세")
    # 토론 이력 밖의 골격(앞부분)은 동일하다.
    assert swapped.split("**Debate History:**")[0] == control.split("**Debate History:**")[0]


def test_corrected_condition_registered():
    """corrected 조건(편향검증 Phase 2 — 교정된 운영 프롬프트)이 등록돼 있다."""
    assert "corrected" in bias_probe.CONDITIONS


def test_corrected_prompt_uses_live_corrected_wording(state):
    """corrected 조건이 교정된 실제 운영 프롬프트(live prompt)를 쓰는지 검증.

    build_research_manager_prompt를 직접 호출하므로, 교정 문구(기저율 균형)가
    있고 반Hold 문구가 없으며, 루브릭 점수 표 섹션이 Recommendation보다 앞에
    온다 (편향검증 Phase 2).
    """
    prompt = bias_probe.build_prompt(state, "corrected")
    # 교정 문구 — research_manager.py의 현행 프롬프트에서 직접 온다.
    assert "Rate in proportion to the evidence" in prompt
    assert "roughly equally common" in prompt
    # 반Hold 문구(Phase 0 발견 2곳)는 없어야 한다.
    assert bias_probe.ANTI_HOLD_SENTENCE not in prompt
    assert bias_probe.SCHEMA_ANTI_HOLD_SENTENCE not in prompt
    # 점수 선출력: 루브릭 점수 섹션이 Recommendation 섹션보다 앞에 온다.
    assert prompt.index("**Rubric Scores**") < prompt.index("**Recommendation**")
    # 증거·토론 이력이 live 빌더를 통해 포함된다.
    assert state["market_report"] in prompt
    assert "개시 강세 주장입니다" in prompt
    # 자유 텍스트 등급 파싱을 위한 마지막 등급 줄 지시가 있다.
    assert "Rating: <X>" in prompt


def test_score_first_prompt_forbids_rating_words(state):
    prompt = bias_probe.build_prompt(state, "score-first")
    # 등급 척도·결단 문구·형식 지시(Recommendation)가 없어야 한다.
    assert "**Rating Scale**" not in prompt
    assert bias_probe.ANTI_HOLD_SENTENCE not in prompt
    assert "**Recommendation**" not in prompt
    assert "Rating: <X>" not in prompt
    # JSON 채점 지시와 루브릭은 있어야 한다.
    assert '"evidence_grounding"' in prompt
    assert "**Evaluation Rubric**" in prompt


# ---------------------------------------------------------------------------
# 라벨 중립화 (neutral-label)
# ---------------------------------------------------------------------------


def test_neutralize_labels_basic_replacements():
    text = (
        "Bull Analyst: the bull case is bullish. "
        "Bear Analyst: bears disagree. 강세론자와 약세 분석가의 토론. "
        "Bull의 주장과 Bear가 놓친 부분. "
        "bulletin board는 그대로. 사용자 @bullANDbear69 보존."
    )
    out = bias_probe.neutralize_labels(text)
    assert "Analyst A: the Analyst A case is upward-leaning." in out
    assert "Analyst B: Analyst B disagree." in out
    assert "분석가 A와 분석가 B의 토론" in out
    # 한글 조사가 바로 붙는 경우도 치환된다 (ASCII 경계).
    assert "Analyst A의 주장과 Analyst B가 놓친 부분." in out
    # 경계 보호: 일반 단어·사용자명(증거 원문)은 오치환하지 않는다.
    assert "bulletin board는 그대로." in out
    assert "@bullANDbear69 보존" in out


def test_neutral_label_prompt_has_no_bull_bear_tokens(state):
    prompt = bias_probe.build_prompt(state, "neutral-label")
    residue = re.findall(r"(?i)\bbull\w*|\bbear\w*", prompt)
    assert residue == [], f"bull/bear 잔존: {residue}"
    assert "강세론자" not in prompt and "약세론자" not in prompt
    # 종목명(티커)은 보존된다.
    assert "AAPL" in prompt
    # 토론 라벨이 중립 라벨로 바뀌었다.
    assert "Analyst A: 개시 강세 주장입니다" in prompt


# ---------------------------------------------------------------------------
# 점수 추출 · 등급 변환 (score-first)
# ---------------------------------------------------------------------------


def test_score_to_rating_uniform_boundaries():
    # net ∈ [-30, 30]을 5등분(폭 12): 경계 ±18, ±6. 경계값은 중앙 쪽 등급.
    assert bias_probe.score_to_rating(30) == "Buy"
    assert bias_probe.score_to_rating(19) == "Buy"
    assert bias_probe.score_to_rating(18) == "Overweight"
    assert bias_probe.score_to_rating(7) == "Overweight"
    assert bias_probe.score_to_rating(6) == "Hold"
    assert bias_probe.score_to_rating(0) == "Hold"
    assert bias_probe.score_to_rating(-6) == "Hold"
    assert bias_probe.score_to_rating(-7) == "Underweight"
    assert bias_probe.score_to_rating(-18) == "Underweight"
    assert bias_probe.score_to_rating(-19) == "Sell"
    assert bias_probe.score_to_rating(-30) == "Sell"


def test_extract_scores_from_fenced_json():
    response = (
        "다음은 채점 결과입니다.\n```json\n"
        '{"evidence_grounding": {"bull": 4, "bear": 2, "why": "a"},\n'
        ' "responsiveness": {"bull": 3, "bear": -1, "why": "b"},\n'
        ' "risk_asymmetry": {"bull": -2, "bear": 5, "why": "c"}}\n'
        "```\n"
    )
    scores = bias_probe.extract_scores(response)
    assert scores["evidence_grounding"] == {"bull": 4, "bear": 2}
    # net = (4-2) + (3-(-1)) + (-2-5) = -1 → Hold
    assert bias_probe.net_score(scores) == -1
    assert bias_probe.score_to_rating(bias_probe.net_score(scores)) == "Hold"


def test_extract_scores_clamps_out_of_range_values():
    response = json.dumps({
        "evidence_grounding": {"bull": 9, "bear": -12, "why": ""},
        "responsiveness": {"bull": 0, "bear": 0, "why": ""},
        "risk_asymmetry": {"bull": 0, "bear": 0, "why": ""},
    })
    scores = bias_probe.extract_scores(response)
    assert scores["evidence_grounding"] == {"bull": 5, "bear": -5}


def test_extract_scores_missing_criterion_raises():
    with pytest.raises(KeyError):
        bias_probe.extract_scores('{"evidence_grounding": {"bull": 1, "bear": 1}}')
    with pytest.raises(ValueError):
        bias_probe.extract_scores("JSON이 전혀 없는 응답")


# ---------------------------------------------------------------------------
# run_probe — 인터리브 실행 · JSONL 기록 (LLM 모킹)
# ---------------------------------------------------------------------------


class _FakeLLM:
    """호출 순서를 기록하고 조건에 맞는 형식의 응답을 돌려주는 가짜 LLM."""

    def __init__(self):
        self.calls = []

    def invoke(self, prompt):
        self.calls.append(prompt)
        if '"evidence_grounding"' in prompt:  # score-first 프롬프트
            content = json.dumps({
                "evidence_grounding": {"bull": 5, "bear": 1, "why": ""},
                "responsiveness": {"bull": 4, "bear": 0, "why": ""},
                "risk_asymmetry": {"bull": 4, "bear": 1, "why": ""},
            })
        else:
            content = "**Recommendation**: Overweight\n...\nRating: Overweight"
        return SimpleNamespace(content=content)


def test_run_probe_interleaves_conditions_and_writes_jsonl(state, tmp_path):
    llm = _FakeLLM()
    samples = [
        {"ticker": "AAPL", "date": "2025-09-01", "state": state},
        {"ticker": "MSFT", "date": "2025-09-01", "state": state},
    ]
    conditions = ["control", "score-first"]
    out = tmp_path / "results.jsonl"
    rows = bias_probe.run_probe(llm, samples, conditions, out, workers=1)

    assert len(rows) == 4
    assert len(llm.calls) == 4
    # 표본별 인터리브: 표본1의 모든 조건 → 표본2의 모든 조건 순서.
    assert [(r["ticker"], r["condition"]) for r in rows] == [
        ("AAPL", "control"), ("AAPL", "score-first"),
        ("MSFT", "control"), ("MSFT", "score-first"),
    ]
    # 등급 추출: control은 parse_rating, score-first는 점수 변환 (net=11 → Overweight).
    by_cond = {(r["ticker"], r["condition"]): r for r in rows}
    assert by_cond[("AAPL", "control")]["rating"] == "Overweight"
    assert by_cond[("AAPL", "score-first")]["rating"] == "Overweight"
    assert by_cond[("AAPL", "score-first")]["net_score"] == 11
    # JSONL 파일에도 같은 행이 기록된다.
    written = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(written) == 4
    assert written[0]["rating"] == "Overweight"


def test_run_probe_isolates_failed_samples(state, tmp_path):
    """LLM 예외가 나도 실행이 멈추지 않고 error 행으로 격리된다."""

    class _BoomLLM:
        def invoke(self, prompt):
            raise RuntimeError("boom")

    samples = [{"ticker": "AAPL", "date": "2025-09-01", "state": state}]
    out = tmp_path / "results.jsonl"
    rows = bias_probe.run_probe(_BoomLLM(), samples, ["control"], out, workers=1)
    assert len(rows) == 1
    assert "error" in rows[0] and "boom" in rows[0]["error"]
    assert "rating" not in rows[0]
