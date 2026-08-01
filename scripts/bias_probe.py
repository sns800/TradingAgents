#!/usr/bin/env python3
# =============================================================================
# [스크립트 개요]
# 강세 편향 분리 검증 실험 (Phase 1) — 리서치 매니저 재판정 프로브.
#
# 배경: 백테스트-실험-결과.md — 전체 파이프라인 등급의 96%(26/27)가 강세,
# 같은 모델의 단일 LLM 호출은 8/11/8로 균형. 모델이 아니라 파이프라인
# 구조·프롬프트가 편향의 뿌리라는 가설을 조건별로 분리 검증한다.
#
# 방법: 백테스트가 남긴 27개 full_states_log JSON(분석가 4종 보고서 +
# Bull/Bear 토론 전체 이력)을 로드해, **리서치 매니저의 판정 프롬프트만**
# 5가지 조건으로 재구성해 LLM을 다시 호출하고 등급 분포를 비교한다.
#
#   1. control      — Phase 2 교정 이전의 research_manager.py 프롬프트를 재현
#                     (역사적 대조군 — 아래 상수들은 의도적으로 동결됨)
#   2. order-swap   — 토론 이력의 Bull/Bear 발언 블록 제시 순서만 반전
#   3. no-anti-hold — Hold 회피/결단 강요 문구(Phase 0 발견 2곳) 제거
#   4. neutral-label— Bull/Bear 라벨과 bull/bear 어휘를 Analyst A/B로 중립화
#   5. score-first  — 등급 단어를 먼저 말하지 못하게 루브릭 3항목별 -5~+5
#                     점수(JSON)만 받고 스크립트가 결정론적으로 등급 변환
#   6. corrected    — [편향검증 Phase 2] 교정된 실제 운영 프롬프트
#                     (research_manager.build_research_manager_prompt를 직접
#                     호출 = live prompt) + 교정된 스키마(점수 필드가
#                     recommendation보다 앞)를 자유 텍스트로 재현한 형식 지시
#
# 재현에 관한 명시적 참고 사항 (control 조건의 알려진 편차):
#   - past_context(과거 교훈)와 verified_snapshot(검증 스냅샷)은 상태 로그에
#     저장되지 않으므로 생략된다. 원본 실행에서도 두 값이 비어 있으면 섹션
#     전체가 생략되는 동일한 가드가 있어, "빈 값이었던 실행"과는 동일하다.
#   - 원본은 구조화 출력(ResearchPlan 스키마)을 우선 시도하지만, 프로브는
#     조건 간 비교 일관성을 위해 5조건 모두 자유 텍스트 경로를 쓰고
#     스키마의 섹션 구조(Recommendation/Bull/Bear/Rationale/Actions)와
#     recommendation 필드의 지시문(anti-hold 포함)을 프롬프트로 재현한다.
#
# 실행 예:
#   TRADINGAGENTS_LLM_PROVIDER=bedrock \
#   TRADINGAGENTS_DEEP_THINK_LLM=us.anthropic.claude-sonnet-4-5-20250929-v1:0 \
#   AWS_DEFAULT_REGION=us-east-1 \
#   .venv/bin/python scripts/bias_probe.py
#
#   .venv/bin/python scripts/bias_probe.py --dry-run --limit 2   # 프롬프트만 생성
# =============================================================================
"""리서치 매니저 재판정 프로브: 강세 편향의 원인을 5조건 A/B로 분리 검증한다."""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as _dt
import functools
import json
import re
import socket
import sys
import time
from pathlib import Path
from typing import Any

# 스크립트를 저장소 루트 밖에서 실행해도 tradingagents 패키지를 찾을 수 있게 한다.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tradingagents.agents.utils.agent_utils import (  # noqa: E402
    get_instrument_context_from_state,
    get_language_instruction,
    get_verified_snapshot_block,
)
from tradingagents.agents.utils.rating import RATINGS_5_TIER, parse_rating  # noqa: E402
from tradingagents.agents.utils.structured import (  # noqa: E402
    NO_EXTERNAL_TOOLS,
    RATING_LINE_INSTRUCTION,
)

# ---------------------------------------------------------------------------
# 조건 정의
# ---------------------------------------------------------------------------

CONDITIONS: tuple[str, ...] = (
    "control",
    "order-swap",
    "no-anti-hold",
    "neutral-label",
    "score-first",
    "corrected",
)

# 기본 호출 예산: 27표본 × 6조건 = 162. 시작 전에 출력하고 절대 초과하지 않는다.
DEFAULT_MAX_CALLS = 162

# ---------------------------------------------------------------------------
# 프롬프트 구성 요소 — Phase 2 교정 *이전*의 research_manager.py f-string 원문.
# (LLM 프롬프트이므로 영어 원문 유지. Phase 1 대조군 재현을 위해 의도적으로
# 동결한다 — 현행 운영 프롬프트는 corrected 조건이
# research_manager.build_research_manager_prompt에서 직접 가져온다.)
# ---------------------------------------------------------------------------

RM_PREAMBLE = (
    "As the Research Manager and debate facilitator, your role is to critically "
    "evaluate this round of debate and deliver a clear, actionable investment "
    "plan for the trader."
)

RATING_SCALE_BLOCK = """**Rating Scale** (use exactly one):
- **Buy**: Strong conviction in the bull thesis; recommend taking or growing the position
- **Overweight**: Constructive view; recommend gradually increasing exposure
- **Hold**: Balanced view; recommend maintaining the current position
- **Underweight**: Cautious view; recommend trimming exposure
- **Sell**: Strong conviction in the bear thesis; recommend exiting or avoiding the position"""

# [Phase 0 발견 (a)-1] research_manager.py:109 — Hold 회피/결단 강요 문구.
# no-anti-hold 조건에서 이 문단을 통째로 제거한다.
ANTI_HOLD_SENTENCE = (
    "Commit to a clear stance whenever the debate's strongest arguments warrant "
    "one; reserve Hold for situations where the evidence on both sides is "
    "genuinely balanced."
)

# [Phase 0 발견 (a)-2] schemas.py ResearchPlan.recommendation 필드 설명의
# anti-hold 문구. 원본은 구조화 출력 스키마를 통해 주입되므로, 자유 텍스트로
# 재현하는 프로브에서는 출력 형식 지시문에 같은 문장을 실어 재현하고
# no-anti-hold 조건에서 함께 제거한다.
SCHEMA_ANTI_HOLD_SENTENCE = (
    " Reserve Hold for situations where the evidence on both sides is genuinely "
    "balanced; otherwise commit to the side with the stronger arguments."
)

RUBRIC_BLOCK = """**Evaluation Rubric** (judge argument quality, not rhetoric — apply each criterion to both sides):
1. **Evidence grounding**: Is each side's core claim backed by specific numbers or facts from the analyst reports below? Discount any claim you cannot trace back to a report.
2. **Responsiveness**: Did each side actually engage with the other's strongest argument? An argument that was never answered still stands; a rebuttal that dodges the point does not count as an answer. Discount claims that were challenged and left unanswered.
3. **Risk asymmetry**: Weigh the magnitude of being wrong on each side — the downside if the bull case fails versus the opportunity cost if the bear case fails — not merely the number of arguments raised."""

# score-first 전용 채점 지시: 등급 단어를 어디에도 쓰지 못하게 하고,
# 루브릭 3항목 × 양측 -5~+5 정수 점수를 JSON으로만 받는다.
SCORE_FIRST_TASK_BLOCK = """**Scoring Task** (do NOT state or imply any investment rating word — such as buy, sell, hold, overweight, underweight, 매수, 매도, 보유 — anywhere in your output):
For each rubric criterion below, score BOTH sides on how well their case performs on that criterion, as an integer from -5 (completely fails the criterion) to +5 (excels at the criterion). Judge argument quality, not rhetoric.

Respond with a single JSON object and nothing else, in exactly this shape:
{"evidence_grounding": {"bull": <int>, "bear": <int>, "why": "<one sentence>"},
 "responsiveness": {"bull": <int>, "bear": <int>, "why": "<one sentence>"},
 "risk_asymmetry": {"bull": <int>, "bear": <int>, "why": "<one sentence>"}}"""

# 원본의 구조화 출력(ResearchPlan)을 자유 텍스트로 재현하는 출력 형식 지시.
# 섹션 이름과 순서는 render_research_plan과 동일하게 유지한다.
# {schema_anti_hold} 자리에 SCHEMA_ANTI_HOLD_SENTENCE가 들어간다(control 계열).
STRUCTURED_FORMAT_TEMPLATE = """

Respond in markdown with exactly these sections, in this order:
**Recommendation**: exactly one of Buy / Overweight / Hold / Underweight / Sell.{schema_anti_hold}
**Bull Case Assessment**: the bull side's strongest claim, whether it is backed by specific evidence from the analyst reports, and whether they actually answered the bear's strongest counterargument. Two to four sentences.
**Bear Case Assessment**: the same assessment for the bear side. Two to four sentences.
**Rationale**: why the winning side's argument quality earned the recommendation, citing the decisive evidence.
**Strategic Actions**: concrete, actionable steps for the trader."""

# [편향검증 Phase 2] 교정된 ResearchPlan 스키마(schemas.py)를 자유 텍스트로
# 재현하는 출력 형식 지시. 핵심: 루브릭 점수 표가 Recommendation보다 먼저
# 오고(스키마 필드 순서와 동일), recommendation은 점수와 정합해야 한다는
# 소프트 결합 지시를 싣는다. schemas.py의 ResearchPlan이 바뀌면 함께 갱신할 것.
CORRECTED_FORMAT_TEMPLATE = """

Respond in markdown with exactly these sections, in this order:
**Rubric Scores**: before stating any rating, score BOTH sides on each rubric criterion (evidence grounding, responsiveness, risk asymmetry) as an integer from -5 (completely fails the criterion) to +5 (excels at it). Format as a markdown table with columns Criterion / Bull / Bear.
**Recommendation**: exactly one of Buy / Overweight / Hold / Underweight / Sell. Rate in proportion to the evidence and keep it consistent with the rubric scores above: Hold is a legitimate finding when the evidence is genuinely balanced, and a directional rating requires the scores to show a clear advantage for that side. If the score totals are roughly even, a directional rating requires explicit justification in the rationale.
**Bull Case Assessment**: the bull side's strongest claim, whether it is backed by specific evidence from the analyst reports, and whether they actually answered the bear's strongest counterargument. Two to four sentences.
**Bear Case Assessment**: the same assessment for the bear side. Two to four sentences.
**Rationale**: why the recommendation follows from the rubric scores and the decisive evidence.
**Strategic Actions**: concrete, actionable steps for the trader."""


# ---------------------------------------------------------------------------
# 토론 이력 조작 (order-swap)
# ---------------------------------------------------------------------------

# 토론 이력에서 발언 블록의 시작을 찾는 패턴. bull/bear 리서처 노드가
# "\n" + "Bull Analyst: ..." 형태로 이어 붙이므로 줄 시작의 화자 라벨이 경계다.
_SPEAKER_RE = re.compile(r"(?m)^(Bull Analyst|Bear Analyst): ")


def split_debate_blocks(history: str) -> list[str]:
    """토론 이력을 화자 라벨 기준의 발언 블록 리스트로 나눈다.

    각 블록은 "Bull Analyst: ..." / "Bear Analyst: ..." 로 시작하는 원문
    그대로이며, 라벨 앞의 서두 텍스트(보통 빈 문자열)는 버린다.
    """
    starts = [m.start() for m in _SPEAKER_RE.finditer(history)]
    if not starts:
        return [history] if history.strip() else []
    blocks = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(history)
        blocks.append(history[start:end].rstrip("\n"))
    return blocks


def swap_debate_order(history: str) -> str:
    """발언 블록의 제시 순서만 반전한다 (각 블록 내부 원문은 그대로 유지).

    예: [Bull#1, Bear#1, Bull#2] → [Bull#2, Bear#1, Bull#1].
    원본 파이프라인은 Bull이 첫 발언과 마지막 발언(2N+1 재반박 보장)을 모두
    가지므로, 반전하면 '마지막 발언' 위치(최신성 이점)가 반대쪽 진영의
    발언으로 넘어가는 효과를 검증할 수 있다.
    """
    blocks = split_debate_blocks(history)
    if len(blocks) < 2:
        return history
    return "\n".join(reversed(blocks))


# ---------------------------------------------------------------------------
# 라벨 중립화 (neutral-label)
# ---------------------------------------------------------------------------

# 치환 규칙: 긴 구문 우선 → 짧은 단어 순으로 적용해 이중 치환을 막는다.
# - 역할 라벨(Bull/Bear Analyst·Researcher)은 Analyst A/B로.
# - bullish/bearish(시장 방향 어휘)는 의미를 보존하는 중립 표현으로 —
#   진영 프레임 토큰(bull/bear)만 제거하고 증거의 방향성 정보는 유지한다.
# - 남은 단독 bull/bear(거의 전부 "the bull case" 같은 진영 지칭)는 Analyst A/B로.
# - 한국어 역할 명칭(강세론자/약세론자, 강세·강기 분석가 등)도 치환한다.
#   단, 단독 "강세"/"약세"(예: "강세 신호")는 시장 방향 서술이므로 유지 —
#   증거 텍스트를 왜곡하지 않기 위한 의도적 선택이다.
# - 단어 경계로 유니코드 \b 대신 ASCII 전용 lookaround를 쓴다: 토론 텍스트가
#   한국어라 "Bull의"처럼 조사가 바로 붙는데, 한글도 \w에 포함되어 \b가
#   성립하지 않기 때문. 앞뒤가 영숫자·밑줄(그리고 앞의 @)이 아닐 때만
#   치환하므로 일반 단어(bulletin)·사용자명(@bullANDbear69)·종목명은
#   오치환되지 않는다 — 사용자명은 증거 원문이므로 보존이 의도된 동작이다.


def _en_token(pattern: str) -> re.Pattern[str]:
    """영어 토큰용 ASCII 경계 패턴을 만든다 (한글 조사는 경계로 취급)."""
    return re.compile(rf"(?<![A-Za-z0-9_@]){pattern}(?![A-Za-z0-9_])", re.IGNORECASE)


_NEUTRAL_SUBS: tuple[tuple[re.Pattern[str], str], ...] = (
    (_en_token(r"bull[\s-]+(?:analyst|researcher)s?"), "Analyst A"),
    (_en_token(r"bear[\s-]+(?:analyst|researcher)s?"), "Analyst B"),
    (_en_token(r"bullish"), "upward-leaning"),
    (_en_token(r"bearish"), "downward-leaning"),
    (_en_token(r"bulls?"), "Analyst A"),
    (_en_token(r"bears?"), "Analyst B"),
    (re.compile(r"강세론자"), "분석가 A"),
    (re.compile(r"약세론자"), "분석가 B"),
    (re.compile(r"[강][세기]\s*분석가"), "분석가 A"),
    (re.compile(r"[약][세기]\s*분석가"), "분석가 B"),
    (re.compile(r"강세\s*측"), "분석가 A 측"),
    (re.compile(r"약세\s*측"), "분석가 B 측"),
)


def neutralize_labels(text: str) -> str:
    """프롬프트/토론 텍스트의 bull/bear 진영 어휘를 중립 라벨로 치환한다."""
    for pattern, repl in _NEUTRAL_SUBS:
        text = pattern.sub(repl, text)
    return text


# ---------------------------------------------------------------------------
# score-first: JSON 점수 추출 + 결정론적 등급 변환
# ---------------------------------------------------------------------------

SCORE_CRITERIA = ("evidence_grounding", "responsiveness", "risk_asymmetry")

# 등급 경계 (코드에 명시된 결정론적 변환):
# net = Σ_{3개 루브릭 항목} (bull 점수 - bear 점수), 항목별 점수는 -5~+5 이므로
# net ∈ [-30, +30]. 이 구간을 5등분(폭 12)한 균등 경계를 쓴다:
#   net >  +18          → Buy
#   +6 <  net ≤ +18     → Overweight
#   -6 ≤  net ≤ +6      → Hold
#   -18 ≤ net <  -6     → Underweight
#   net <  -18          → Sell
# (±6, ±18 경계값은 중앙에 가까운 쪽 등급에 귀속 — Hold/Overweight/Underweight)


def score_to_rating(net: float) -> str:
    """합산 점수(net)를 5단계 등급으로 결정론적으로 변환한다."""
    if net > 18:
        return "Buy"
    if net > 6:
        return "Overweight"
    if net >= -6:
        return "Hold"
    if net >= -18:
        return "Underweight"
    return "Sell"


def _clamp_score(value: Any) -> int:
    """점수를 -5~+5 정수로 강제한다 (범위 밖 값은 경계로 절단)."""
    return max(-5, min(5, int(value)))


def extract_scores(response_text: str) -> dict[str, dict[str, int]]:
    """LLM 응답에서 점수 JSON을 추출해 {항목: {bull, bear}} 형태로 반환한다.

    코드펜스(```json ... ```)나 앞뒤 잡담이 섞여도 첫 '{'부터 마지막 '}'까지를
    JSON으로 파싱한다. 필수 항목이 없으면 KeyError/ValueError를 던져 호출자가
    실패 표본으로 격리하게 한다.
    """
    start = response_text.find("{")
    end = response_text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no JSON object found in response")
    data = json.loads(response_text[start : end + 1])
    scores: dict[str, dict[str, int]] = {}
    for criterion in SCORE_CRITERIA:
        entry = data[criterion]  # 없으면 KeyError → 실패 표본으로 격리
        scores[criterion] = {
            "bull": _clamp_score(entry["bull"]),
            "bear": _clamp_score(entry["bear"]),
        }
    return scores


def net_score(scores: dict[str, dict[str, int]]) -> int:
    """루브릭 3항목의 (bull - bear) 점수 합을 반환한다. 범위: [-30, +30]."""
    return sum(s["bull"] - s["bear"] for s in scores.values())


# ---------------------------------------------------------------------------
# 프롬프트 재구성
# ---------------------------------------------------------------------------


def build_prompt(state: dict[str, Any], condition: str) -> str:
    """상태 로그 하나로부터 지정 조건의 리서치 매니저 판정 프롬프트를 만든다."""
    if condition not in CONDITIONS:
        raise ValueError(f"unknown condition: {condition}")

    if condition == "corrected":
        # [편향검증 Phase 2] 교정된 실제 운영 프롬프트(live prompt)를 그대로
        # 사용한다: research_manager의 프롬프트 빌더를 직접 호출하고, 구조화
        # 출력 스키마(점수 필드 → recommendation 순서)는 자유 텍스트 형식
        # 지시로 재현한다. 다른 조건과 동일하게 등급 줄 지시를 덧붙인다.
        from tradingagents.agents.managers.research_manager import (
            build_research_manager_prompt,
        )

        return (
            build_research_manager_prompt(state)
            + CORRECTED_FORMAT_TEMPLATE
            + RATING_LINE_INSTRUCTION
        )

    instrument_context = get_instrument_context_from_state(state)
    history = state["investment_debate_state"].get("history", "")

    market_research_report = state.get("market_report", "")
    sentiment_report = state.get("sentiment_report", "")
    news_report = state.get("news_report", "")
    fundamentals_report = state.get("fundamentals_report", "")

    # 상태 로그에 verified_snapshot/past_context가 없으면 원본과 동일한 가드로
    # 섹션 전체가 생략된다 (백테스트 로그에는 두 키가 저장되지 않음 — 모듈
    # 상단 주석 참고).
    snapshot_block = get_verified_snapshot_block(state)
    past_context = state.get("past_context", "")
    lessons_block = (
        "**Lessons from past decisions and their outcomes** (reflections from "
        "already-resolved calls — consult them when judging this debate: check "
        "whether either side is repeating a mistake flagged below and weigh their "
        "arguments accordingly; do not let past ratings anchor your new rating):\n"
        f"{past_context}\n\n---\n\n"
        if past_context
        else ""
    )

    if condition == "order-swap":
        history = swap_debate_order(history)

    lang = get_language_instruction()

    if condition == "score-first":
        # 등급 단어를 먼저 말하지 못하게: 등급 척도·결단 문구·형식 지시를 모두
        # 빼고, 루브릭 점수 JSON만 받는다. 등급은 score_to_rating이 계산한다.
        preamble = (
            "As the Research Manager and debate facilitator, your role is to "
            "critically evaluate this round of debate by scoring each side "
            "against the evaluation rubric."
        )
        return f"""{preamble}

{instrument_context}

---

{SCORE_FIRST_TASK_BLOCK}

---

{RUBRIC_BLOCK}

---

**Analyst Reports** (original evidence — cross-check the debaters' claims against these reports and discount claims they do not support; a report may be empty if that analyst was not run):
Market Research Report: {market_research_report}
Social Media Sentiment Report: {sentiment_report}
Latest World Affairs Report: {news_report}
Company Fundamentals Report: {fundamentals_report}

{snapshot_block}---

{lessons_block}**Debate History:**
{history}

{NO_EXTERNAL_TOOLS}{lang}"""

    # control / order-swap / no-anti-hold / neutral-label 공통 골격.
    # no-anti-hold: Phase 0 발견 (a)-1·(a)-2의 anti-hold 문구 2곳만 제거.
    anti_hold_paragraph = f"\n\n{ANTI_HOLD_SENTENCE}" if condition != "no-anti-hold" else ""
    schema_anti_hold = SCHEMA_ANTI_HOLD_SENTENCE if condition != "no-anti-hold" else ""
    format_instruction = STRUCTURED_FORMAT_TEMPLATE.format(schema_anti_hold=schema_anti_hold)

    prompt = f"""{RM_PREAMBLE}

{instrument_context}

---

{RATING_SCALE_BLOCK}{anti_hold_paragraph}

---

{RUBRIC_BLOCK}

---

**Analyst Reports** (original evidence — cross-check the debaters' claims against these reports and discount claims they do not support; a report may be empty if that analyst was not run):
Market Research Report: {market_research_report}
Social Media Sentiment Report: {sentiment_report}
Latest World Affairs Report: {news_report}
Company Fundamentals Report: {fundamentals_report}

{snapshot_block}---

{lessons_block}**Debate History:**
{history}

{NO_EXTERNAL_TOOLS}{lang}{format_instruction}{RATING_LINE_INSTRUCTION}"""

    if condition == "neutral-label":
        # 프롬프트 전체(지시문 + 등급 척도 + 보고서 + 토론 이력)에 라벨
        # 중립화를 적용한다. 등급 척도의 "bull thesis" 등도 함께 치환된다.
        prompt = neutralize_labels(prompt)

    return prompt


# ---------------------------------------------------------------------------
# 표본 로드
# ---------------------------------------------------------------------------


def load_samples(log_root: Path, tickers: list[str] | None = None) -> list[dict[str, Any]]:
    """백테스트 상태 로그를 모두 로드해 (ticker, date, state) 표본 리스트로 만든다."""
    samples = []
    ticker_dirs = sorted(
        d for d in log_root.iterdir()
        if d.is_dir() and (tickers is None or d.name in tickers)
    )
    for ticker_dir in ticker_dirs:
        state_dir = ticker_dir / "TradingAgentsStrategy_logs"
        if not state_dir.is_dir():
            continue
        for path in sorted(state_dir.glob("full_states_log_*.json")):
            date = path.stem.replace("full_states_log_", "")
            with open(path, encoding="utf-8") as f:
                state = json.load(f)
            samples.append({"ticker": ticker_dir.name, "date": date, "state": state})
    return samples


# ---------------------------------------------------------------------------
# 실행 루프
# ---------------------------------------------------------------------------


def _call_condition(llm: Any, sample: dict[str, Any], condition: str) -> dict[str, Any]:
    """표본 하나 × 조건 하나를 실행해 결과 행(dict)을 반환한다. 예외는 격리한다."""
    row: dict[str, Any] = {
        "ticker": sample["ticker"],
        "date": sample["date"],
        "condition": condition,
    }
    try:
        prompt = build_prompt(sample["state"], condition)
        row["prompt_chars"] = len(prompt)
        t0 = time.monotonic()
        response = llm.invoke(prompt)
        row["elapsed_s"] = round(time.monotonic() - t0, 1)
        text = response.content if isinstance(response.content, str) else str(response.content)
        row["decision"] = text
        if condition == "score-first":
            scores = extract_scores(text)
            row["scores"] = scores
            row["net_score"] = net_score(scores)
            row["rating"] = score_to_rating(row["net_score"])
        else:
            row["rating"] = parse_rating(text, context=f"bias_probe:{condition}")
    except Exception as exc:  # noqa: BLE001 — 실패 표본은 격리하고 실험을 계속한다
        row["error"] = f"{type(exc).__name__}: {exc}"
    return row


def run_probe(
    llm: Any,
    samples: list[dict[str, Any]],
    conditions: list[str],
    out_jsonl: Path,
    workers: int = 5,
) -> list[dict[str, Any]]:
    """표본별로 조건들을 인터리브 실행하고 결과를 JSONL로 즉시 기록한다.

    시간대 편향 제거: 표본 하나의 모든 조건을 (병렬로) 함께 실행한 뒤 다음
    표본으로 넘어가므로, 실행 시각의 드리프트가 특정 조건에 몰리지 않는다.
    """
    rows: list[dict[str, Any]] = []
    with open(out_jsonl, "a", encoding="utf-8") as f:
        for i, sample in enumerate(samples, 1):
            sample_rows: list[dict[str, Any]]
            if workers > 1:
                with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                    sample_rows = list(
                        pool.map(functools.partial(_call_condition, llm, sample), conditions)
                    )
            else:
                sample_rows = [_call_condition(llm, sample, c) for c in conditions]
            for row in sample_rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            rows.extend(sample_rows)
            done = ", ".join(
                f"{r['condition']}={r.get('rating', 'ERR')}" for r in sample_rows
            )
            print(f"[{i}/{len(samples)}] {sample['ticker']} {sample['date']}: {done}", flush=True)
    return rows


# ---------------------------------------------------------------------------
# 결과 요약 (마크다운)
# ---------------------------------------------------------------------------

BULLISH = {"Buy", "Overweight"}
BEARISH = {"Underweight", "Sell"}


def summarize(rows: list[dict[str, Any]], conditions: list[str]) -> str:
    """조건별 등급 분포·강세 비율·control 대비 변화를 마크다운으로 요약한다."""
    by_cond: dict[str, dict[str, str]] = {c: {} for c in conditions}
    for row in rows:
        if "rating" in row:
            by_cond[row["condition"]][f"{row['ticker']}_{row['date']}"] = row["rating"]

    control = by_cond.get("control", {})
    lines = [
        "# bias_probe 결과 요약",
        "",
        f"생성 시각: {_dt.datetime.now().isoformat(timespec='seconds')}",
        "",
        "참고: past_context(과거 교훈)와 verified_snapshot(검증 스냅샷)은 상태 로그에",
        "저장되지 않아 모든 조건에서 생략됨. 원본은 구조화 출력, 프로브는 자유 텍스트",
        "경로(5조건 동일)로 실행됨.",
        "",
        "| 조건 | n | Buy | Overweight | Hold | Underweight | Sell | 강세% | Δ강세%p (vs control) | 등급 변경 (vs control) | 실패 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    n_errors = {c: sum(1 for r in rows if r["condition"] == c and "error" in r) for c in conditions}
    control_bull_pct = None
    for cond in conditions:
        ratings = by_cond[cond]
        n = len(ratings)
        counts = {tier: sum(1 for v in ratings.values() if v == tier) for tier in RATINGS_5_TIER}
        bull_pct = (100 * sum(counts[t] for t in BULLISH) / n) if n else 0.0
        if cond == "control":
            control_bull_pct = bull_pct
        delta = (
            f"{bull_pct - control_bull_pct:+.1f}"
            if control_bull_pct is not None and cond != "control"
            else "—"
        )
        # 표본 쌍 단위 등급 변경 건수: control과 해당 조건이 모두 성공한 표본 중
        # 등급이 달라진 개수.
        if cond == "control":
            changed = "—"
        else:
            pairs = [k for k in ratings if k in control]
            changed = f"{sum(1 for k in pairs if ratings[k] != control[k])}/{len(pairs)}"
        lines.append(
            f"| {cond} | {n} | {counts['Buy']} | {counts['Overweight']} | {counts['Hold']} | "
            f"{counts['Underweight']} | {counts['Sell']} | {bull_pct:.1f}% | {delta} | {changed} | "
            f"{n_errors[cond]} |"
        )

    # control 대비 등급이 바뀐 표본 목록 (질적 분석용)
    lines += ["", "## control 대비 등급 변경 표본", ""]
    for cond in conditions:
        if cond == "control":
            continue
        changed_keys = [
            k for k, v in by_cond[cond].items() if k in control and v != control[k]
        ]
        lines.append(f"### {cond} ({len(changed_keys)}건)")
        for k in sorted(changed_keys):
            lines.append(f"- {k}: {control[k]} → {by_cond[cond][k]}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="리서치 매니저 재판정 프로브 — 강세 편향 원인 분리 검증 (Phase 1)",
    )
    parser.add_argument(
        "--log-root",
        default=str(Path.home() / ".tradingagents" / "logs" / "backtest"),
        help="백테스트 상태 로그 루트 (기본: ~/.tradingagents/logs/backtest)",
    )
    parser.add_argument(
        "--tickers", default=None,
        help="쉼표 구분 티커 필터 (기본: 로그 루트의 모든 티커)",
    )
    parser.add_argument(
        "--conditions", default=",".join(CONDITIONS),
        help=f"쉼표 구분 조건 목록 (기본: {','.join(CONDITIONS)})",
    )
    parser.add_argument("--limit", type=int, default=None, help="표본 수 상한")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="LLM 호출 없이 프롬프트만 생성해 out 디렉터리에 저장",
    )
    parser.add_argument(
        "--max-calls", type=int, default=DEFAULT_MAX_CALLS,
        help=f"총 LLM 호출 수 상한 (기본: {DEFAULT_MAX_CALLS}, 초과 시 시작 전 중단)",
    )
    parser.add_argument(
        "--workers", type=int, default=5,
        help="표본 내 조건 병렬 실행 수 (기본: 5 — 표본별 인터리브는 유지됨)",
    )
    parser.add_argument("--out", default=None, help="결과 출력 디렉터리")
    args = parser.parse_args()

    # 전역 소켓 타임아웃: 타임아웃 없는 HTTP 호출로 인한 무한 대기 방지
    # (백테스트-실험-결과.md 실행 노트의 재발 방지책과 동일).
    socket.setdefaulttimeout(120)

    conditions = [c.strip() for c in args.conditions.split(",") if c.strip()]
    unknown = [c for c in conditions if c not in CONDITIONS]
    if unknown:
        print(f"오류: 알 수 없는 조건 {unknown} (가능: {list(CONDITIONS)})")
        return 1

    tickers = [t.strip() for t in args.tickers.split(",")] if args.tickers else None
    samples = load_samples(Path(args.log_root), tickers)
    if args.limit is not None:
        samples = samples[: args.limit]
    if not samples:
        print("오류: 상태 로그 표본이 없습니다.")
        return 1

    total_calls = len(samples) * len(conditions)
    print(f"표본 {len(samples)}개 × 조건 {len(conditions)}개 = 총 호출 수 {total_calls}")
    if not args.dry_run and total_calls > args.max_calls:
        print(
            f"오류: 총 호출 수 {total_calls}가 상한 {args.max_calls}을 초과합니다. "
            "--limit 또는 --conditions로 줄이세요."
        )
        return 1

    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out) if args.out else (
        Path.home() / ".tradingagents" / "logs" / "bias_probe" / f"run_{ts}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"출력 디렉터리: {out_dir}")

    if args.dry_run:
        # 프롬프트만 생성해 파일로 저장 — 조건별 재구성 검증용 (LLM 호출 없음).
        prompt_dir = out_dir / "prompts"
        prompt_dir.mkdir(exist_ok=True)
        for sample in samples:
            for cond in conditions:
                path = prompt_dir / f"{sample['ticker']}_{sample['date']}_{cond}.txt"
                path.write_text(build_prompt(sample["state"], cond), encoding="utf-8")
        print(f"dry-run 완료: {len(samples) * len(conditions)}개 프롬프트 저장 → {prompt_dir}")
        return 0

    # LLM 클라이언트 — 파이프라인과 동일한 팩토리 + deep_think_llm 사용.
    # 제공자/모델/온도는 TRADINGAGENTS_* 환경 변수로 지정한다 (기본 설정과 병합).
    from tradingagents.dataflows.config import get_config
    from tradingagents.llm_clients import create_llm_client

    config = get_config()
    llm_kwargs: dict[str, Any] = {}
    temperature = config.get("temperature")
    if temperature is not None and temperature != "":
        llm_kwargs["temperature"] = float(temperature)
    client = create_llm_client(
        provider=config["llm_provider"],
        model=config["deep_think_llm"],
        base_url=config.get("backend_url"),
        **llm_kwargs,
    )
    llm = client.get_llm()
    print(f"LLM: provider={config['llm_provider']} model={config['deep_think_llm']}")

    out_jsonl = out_dir / "results.jsonl"
    rows = run_probe(llm, samples, conditions, out_jsonl, workers=max(1, args.workers))

    summary = summarize(rows, conditions)
    summary_path = out_dir / "summary.md"
    summary_path.write_text(summary, encoding="utf-8")
    print()
    print(summary)
    print(f"\n결과: {out_jsonl}\n요약: {summary_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
