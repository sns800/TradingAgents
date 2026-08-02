# ============================================================================
# 리서치 매니저(Research Manager) 모듈
#
# 이 에이전트는 강세론자(Bull)/약세론자(Bear) 리서처들의 토론을 비판적으로
# 평가하고, 그 결과를 트레이더가 실행할 수 있는 구조화된 투자 계획
# (investment_plan)으로 정리하는 역할을 합니다.
# 전체 파이프라인(분석가 → 리서처 토론 → 트레이더 → 리스크 토론 → 포트폴리오 매니저)에서
# 리서처 토론 단계의 판정자(Judge)로 위치하며, 여기서 만든 투자 계획은
# 곧바로 트레이더의 거래 제안 작성에 입력됩니다.
# ============================================================================

"""리서치 매니저(Research Manager): 강세/약세 토론을 트레이더용 구조화된 투자 계획으로 변환합니다."""

from __future__ import annotations

from tradingagents.agents.schemas import ResearchPlan, render_research_plan
from tradingagents.agents.utils.agent_utils import (
    get_instrument_context_from_state,
    get_language_instruction,
    get_verified_snapshot_block,
)
from tradingagents.agents.utils.debate_context import condense_for_judge
from tradingagents.agents.utils.structured import (
    NO_EXTERNAL_TOOLS,
    bind_structured,
    invoke_structured_or_freetext,
)


def build_research_manager_prompt(state) -> str:
    """리서치 매니저의 판정 프롬프트를 상태(state)로부터 구성해 반환한다.

    노드 본체와 scripts/bias_probe.py의 `corrected` 조건이 공유하는 단일
    소스(single source of truth)다 — 프로브가 실제 운영 프롬프트와 어긋난
    문구로 검증하는 일을 막는다.
    """
    instrument_context = get_instrument_context_from_state(state)  # 종목/자산 정보 문자열
    # 강세론자(Bull)/약세론자(Bear) 토론 이력. 심판은 판정 근거로 이력이
    # 필요하므로 토론자보다 넉넉히 받되, depth가 크면(2N+1 턴) 이력이 폭증해
    # 한국어 환경에서 모델 입력 한도를 넘겨 "Input is too long" 오류가 났다.
    # condense_for_judge로 총 예산(기본 40k자) 안으로 제한한다 — 예산보다
    # 짧으면(대개 depth 1~3) 원문 그대로라 기존 동작이 보존된다.
    history = condense_for_judge(state["investment_debate_state"].get("history", ""))

    # 분석가 4종 원본 보고서: 심판이 토론자들의 주장을 원자료와 대조해
    # 검증할 수 있도록 리스크 토론자와 동일한 방식으로 제공합니다.
    # 분석가 일부만 선택된 실행에서는 키가 없거나 빈 문자열일 수 있으므로
    # .get()으로 안전하게 꺼냅니다.
    market_research_report = state.get("market_report", "")
    sentiment_report = state.get("sentiment_report", "")
    news_report = state.get("news_report", "")
    fundamentals_report = state.get("fundamentals_report", "")

    # 검증 스냅샷 섹션(중기 로드맵 #5): 정확한 수치 인용의 기준점.
    # 비어 있으면 섹션 전체가 생략된다 (past_context 빈 값 가드와 동일 패턴).
    snapshot_block = get_verified_snapshot_block(state)

    # 과거 결정과 결과에서 얻은 교훈(메모리)이 있으면 판정용 지시문과 함께
    # 프롬프트에 포함합니다. 비어 있으면 섹션 전체를 생략합니다 — 빈 섹션이
    # 존재하지 않는 과거 교훈을 지어내게(hallucinate) 유도하는 것을 막는
    # #572 트레이드오프를 그대로 유지합니다 (PM과 동일한 패턴).
    past_context = state.get("past_context", "")
    # [한국어 요약] 아래 lessons 블록은 LLM에게 다음을 지시합니다:
    # "이미 결과가 확정된 과거 결정들의 반성(REFLECTION)이다. 판정 시 참고하라 —
    # 어느 쪽 토론자가 과거에 지적된 실수를 반복하고 있는지 확인하고 그에 따라
    # 논거의 무게를 조정하라. 과거 등급이 이번 등급을 앵커링하게 하지는 말라."
    lessons_block = (
        "**Lessons from past decisions and their outcomes** (reflections from "
        "already-resolved calls — consult them when judging this debate: check "
        "whether either side is repeating a mistake flagged below and weigh their "
        "arguments accordingly; do not let past ratings anchor your new rating):\n"
        f"{past_context}\n\n---\n\n"
        if past_context
        else ""
    )

    # [한국어 요약] 아래 f-string 프롬프트는 LLM에게 다음을 지시합니다:
    # "리서치 매니저이자 토론 진행자로서 이번 토론 라운드를 비판적으로 평가하고,
    # 트레이더를 위한 명확하고 실행 가능한 투자 계획을 제시하라.
    # 등급(Rating)은 Buy(매수)/Overweight(비중 확대)/Hold(보유)/
    # Underweight(비중 축소)/Sell(매도) 중 정확히 하나를 사용하라.
    # [편향검증 Phase 2] 증거에 비례해 등급을 매겨라 — 대형주의 수많은
    # 종목-일 단위에서 양(+)의 알파와 음(-)의 알파는 대략 비슷하게 흔하므로,
    # 낙관이나 '행동해야 한다'는 충동이 등급을 정하게 하지 말라. 증거가
    # 진정으로 균형이면 Hold는 정당한 판정이며, 방향성 등급을 주려면 루브릭이
    # 그 방향의 뚜렷한 우위를 보여야 한다. (기존의 "결단하라/Hold를 아껴라"
    # 문구는 분리 실험에서 강세 편향의 주범으로 확인되어 교체됨 —
    # ~/.tradingagents/logs/bias_probe/run_main/summary.md)
    # [평가 루브릭 — 중기 로드맵 #3] 수사(말솜씨)가 아닌 논거 품질로
    # 판정하라: (1) 증거 접지 — 각 측의 핵심 주장이 분석가 보고서의 구체
    # 수치·사실로 뒷받침되는가(추적 불가한 주장은 할인), (2) 응답성 —
    # 상대의 최강 논거에 실제로 응답했는가(응답 없이 남은 논거는 유효하고,
    # 논점을 회피한 반박은 응답으로 치지 않으며, 도전받고도 무응답인
    # 주장은 할인), (3) 리스크 비대칭 — 논거 개수가 아니라 각 측이 틀렸을
    # 때의 손실 크기(강세 실패 시 하방 vs 약세 실패 시 기회비용)를 가중하라.
    # [편향검증 Phase 2 — 점수 선출력] 등급을 정하기 전에 루브릭 3항목별로
    # 양측에 -5~+5 정수 점수를 먼저 매기고, recommendation은 그 점수와
    # 정합해야 한다 — 합계가 대등한데 방향 등급을 주려면 rationale에 명시적
    # 근거가 필요하다 (결정론적 강제 변환은 과교정(전원 Hold)이 확인되어
    # 도입하지 않음).
    # 분석가 원본 보고서 4종과 토론 이력, (있다면) 과거 교훈이 컨텍스트로
    # 주어진다. 토론자의 주장은 원본 보고서와 대조해 검증하고, 보고서에
    # 근거가 없는 주장은 낮게 평가하라 (해당 분석가가 실행되지 않았으면
    # 보고서가 비어 있을 수 있다). 외부 도구는 사용하지 말라."
    # ※ 프롬프트를 번역하면 모델 출력 형식이 깨질 수 있어 영어 원문을 유지합니다.
    return f"""As the Research Manager and debate facilitator, your role is to critically evaluate this round of debate and deliver a clear, actionable investment plan for the trader.

{instrument_context}

---

**Rating Scale** (use exactly one):
- **Buy**: Strong conviction in the bull thesis; recommend taking or growing the position
- **Overweight**: Constructive view; recommend gradually increasing exposure
- **Hold**: Balanced view; recommend maintaining the current position
- **Underweight**: Cautious view; recommend trimming exposure
- **Sell**: Strong conviction in the bear thesis; recommend exiting or avoiding the position

Rate in proportion to the evidence. Across many large-cap stock-days, positive and negative alpha are roughly equally common — do not let optimism or the urge to act set your rating. Hold is a legitimate finding when the evidence is genuinely balanced; a directional rating requires the rubric to show a clear advantage for that side.

---

**Evaluation Rubric** (judge argument quality, not rhetoric — apply each criterion to both sides):
1. **Evidence grounding**: Is each side's core claim backed by specific numbers or facts from the analyst reports below? Discount any claim you cannot trace back to a report.
2. **Responsiveness**: Did each side actually engage with the other's strongest argument? An argument that was never answered still stands; a rebuttal that dodges the point does not count as an answer. Discount claims that were challenged and left unanswered.
3. **Risk asymmetry**: Weigh the magnitude of being wrong on each side — the downside if the bull case fails versus the opportunity cost if the bear case fails — not merely the number of arguments raised.

Score the rubric before you rate: assign each side an integer score from -5 to +5 on every criterion above, and only then choose the recommendation. The recommendation must be consistent with those scores — if the score totals are roughly even, giving a directional rating requires explicit justification in your rationale.

---

**Analyst Reports** (original evidence — cross-check the debaters' claims against these reports and discount claims they do not support; a report may be empty if that analyst was not run):
Market Research Report: {market_research_report}
Social Media Sentiment Report: {sentiment_report}
Latest World Affairs Report: {news_report}
Company Fundamentals Report: {fundamentals_report}

{snapshot_block}---

{lessons_block}**Debate History:**
{history}

{NO_EXTERNAL_TOOLS}""" + get_language_instruction()


def create_research_manager(llm):
    # 구조화 출력 바인딩: LLM이 ResearchPlan 스키마 형태로 응답하도록 감쌉니다.
    structured_llm = bind_structured(llm, ResearchPlan, "Research Manager")

    # LangGraph 노드 함수: 상태(state) 딕셔너리를 입력받아
    # 갱신할 키들만 담은 딕셔너리를 반환합니다.
    def research_manager_node(state) -> dict:
        investment_debate_state = state["investment_debate_state"]

        # 판정 프롬프트 구성은 모듈 함수로 분리되어 bias_probe와 공유됩니다.
        prompt = build_research_manager_prompt(state)

        # 구조화 출력을 우선 시도하고, 지원하지 않는 제공자(provider)에서는
        # 자유 텍스트 생성으로 대체(fallback)하여 투자 계획 텍스트를 얻습니다.
        investment_plan = invoke_structured_or_freetext(
            structured_llm,
            llm,
            prompt,
            render_research_plan,
            "Research Manager",
        )

        # 투자 토론 상태(investment_debate_state)를 갱신합니다: 투자 계획을
        # "judge_decision"(판정 결과)과 "current_response"에 기록하고,
        # 기존 강세/약세 토론 이력은 그대로 보존합니다.
        # ※ dict 키 이름은 프로그램 동작에 쓰이므로 절대 변경하면 안 됩니다.
        new_investment_debate_state = {
            "judge_decision": investment_plan,
            "history": investment_debate_state.get("history", ""),
            "bear_history": investment_debate_state.get("bear_history", ""),
            "bull_history": investment_debate_state.get("bull_history", ""),
            "current_response": investment_plan,
            "count": investment_debate_state["count"],
        }

        # 상태(state) 갱신: 갱신된 토론 상태와 투자 계획을 반환합니다.
        # investment_plan은 다음 단계인 트레이더 노드가 사용합니다.
        return {
            "investment_debate_state": new_investment_debate_state,
            "investment_plan": investment_plan,
        }

    return research_manager_node
