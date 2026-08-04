# ============================================================================
# 포트폴리오 매니저(Portfolio Manager) 모듈
#
# 이 에이전트는 공격적(Aggressive)/보수적(Conservative)/중립적(Neutral)
# 리스크 분석가들의 토론을 종합해 최종 거래 결정(final_trade_decision)을
# 내리는 역할을 합니다.
# 전체 파이프라인(분석가 → 리서처 토론 → 트레이더 → 리스크 토론 → 포트폴리오 매니저)에서
# 가장 마지막 단계에 위치하며, 리서치 매니저의 투자 계획과 트레이더의 거래 제안,
# 리스크 토론 이력을 모두 입력받아 최종 판정(Judge) 결과를 산출합니다.
# ============================================================================

"""포트폴리오 매니저(Portfolio Manager): 리스크 분석가 토론을 종합해 최종 결정을 내립니다.

LangChain의 ``with_structured_output`` 을 사용해 LLM이 단 한 번의 호출로
타입이 지정된 ``PortfolioDecision`` 을 직접 생성하게 합니다. 결과는
``final_trade_decision`` 에 저장하기 위해 다시 마크다운으로 렌더링되므로,
메모리 로그, CLI 표시, 저장된 보고서가 지금과 동일한 형태를 계속 사용할 수
있습니다. 제공자(provider)가 구조화 출력을 지원하지 않는 경우에는
자유 텍스트 생성으로 우아하게 대체(fallback)됩니다.
"""

from __future__ import annotations

from tradingagents.agents.schemas import PortfolioDecision, render_pm_decision
from tradingagents.agents.utils.agent_utils import (
    get_horizon_instruction,
    get_instrument_context_from_state,
    get_language_instruction,
    get_verified_snapshot_block,
)
from tradingagents.agents.utils.debate_context import condense_for_judge
from tradingagents.agents.utils.rating import parse_rating
from tradingagents.agents.utils.structured import (
    NO_EXTERNAL_TOOLS,
    bind_structured,
    invoke_structured_or_freetext,
)

# [NO_DATA 결정론적 게이트 — 설계분석 중기 로드맵 #4]
# 핵심 시장 데이터가 없을 때(market_data_ok=False) LLM 호출 없이 기록되는
# 강제 Hold 결정문. "데이터 없이 매수하지 마라"는 프롬프트 순응은 확률적
# 방어일 뿐이므로, 자금이 걸린 결정은 여기서 결정론적으로 차단합니다.
# 렌더링 형식은 구조화 출력 렌더러(render_pm_decision)와 동일한
# "**Rating**:" 헤더를 유지해, 시그널 파서(parse_rating)·CLI·보고서 저장기가
# 정상 경로와 같은 방식으로 소비할 수 있게 합니다. (하류 소비자용 영어 유지)
FORCED_HOLD_REASON = "Insufficient market data - deterministic hold"
FORCED_HOLD_DECISION = (
    "**Rating**: Hold\n\n"
    f"**Executive Summary**: {FORCED_HOLD_REASON}. Core market data for this "
    "instrument could not be retrieved from any configured vendor (a NO_DATA "
    "sentinel was detected in the market analyst's tool results), so this Hold "
    "was issued deterministically without invoking the LLM judge.\n\n"
    "**Investment Thesis**: A money-at-risk decision requires verified market "
    "data. Because no usable market data was available, taking no action is "
    "the only defensible position. Re-run the analysis once market data "
    "becomes available for this symbol."
)


def build_portfolio_manager_prompt(state) -> str:
    """포트폴리오 매니저의 판정 프롬프트를 상태(state)로부터 구성해 반환한다.

    노드 본체와 scripts/pm_probe.py가 공유하는 단일 소스(single source of
    truth)다 — 프로브가 실제 운영 프롬프트와 어긋난 문구로 검증하는 일을 막는다.

    [리스크 감독 게이트 재프레이밍 — BACKLOG.md B2 옵션 b]
    전수 조사 결과 리서치 매니저(RM) → PM의 최종 등급 밴드 변경이 0/40이었다
    (편향검증-실험-결과.md, BACKLOG.md B2). 즉 PM은 RM 판정을 그대로 통과시키는
    "고무도장"이었다. 이를 교정하기 위해 PM을 재종합자(re-synthesizer)에서
    **리스크 감독 게이트**로 재정의한다: RM 등급을 명시적 앵커로 제시하고,
    리스크 토론의 관점에서 그 등급을 확정(confirm)/상향(upgrade)/하향(downgrade)할지
    판정하게 한다. override 기준은 자본 보호 우선의 비대칭 구조다(하향 사유가
    상향 사유보다 넓다).
    """
    instrument_context = get_instrument_context_from_state(state)  # 종목/자산 정보 문자열

    # 상태에서 리스크 토론 이력과 상위 단계 산출물들을 꺼냅니다.
    # 심판은 판정 근거로 이력이 필요하므로 토론자보다 넉넉히 받되, depth가
    # 크면(3N+1 턴) 이력이 폭증해 한국어 환경에서 모델 입력 한도를 넘긴다.
    # condense_for_judge로 총 예산 안으로 제한(예산보다 짧으면 원문 그대로).
    history = condense_for_judge(state["risk_debate_state"]["history"])  # 리스크 토론 이력(예산 제한)
    research_plan = state["investment_plan"]  # 리서치 매니저의 투자 계획
    trader_plan = state["trader_investment_plan"]  # 트레이더의 거래 제안

    # [리스크 감독 게이트] RM 제안 등급을 investment_plan 텍스트에서 결정론적으로
    # 추출해 별도 앵커로 제시한다. 지금까지는 등급이 투자 계획 본문에 묻혀 있어
    # PM이 "무엇을 확정/변경하는지"의 기준점을 명시적으로 인지하지 못했다.
    rm_rating = parse_rating(research_plan, context="portfolio_manager:rm_anchor")

    # 과거 결정과 결과에서 얻은 교훈(메모리)이 있으면 프롬프트에 포함합니다.
    past_context = state.get("past_context", "")
    lessons_line = (
        f"- Lessons from prior decisions and outcomes:\n{past_context}\n"
        if past_context
        else ""
    )

    # 분석가 4종 원본 보고서: 최종 판정자가 토론자들의 주장을 원자료와
    # 대조해 검증할 수 있도록 리스크 토론자와 동일한 방식으로 제공합니다.
    # 분석가 일부만 선택된 실행에서는 키가 없거나 빈 문자열일 수 있으므로
    # .get()으로 안전하게 꺼냅니다.
    market_research_report = state.get("market_report", "")
    sentiment_report = state.get("sentiment_report", "")
    news_report = state.get("news_report", "")
    fundamentals_report = state.get("fundamentals_report", "")

    # 검증 스냅샷 섹션(중기 로드맵 #5): 정확한 수치 인용의 기준점.
    # 비어 있으면 섹션 전체가 생략된다 (past_context 빈 값 가드와 동일 패턴).
    snapshot_block = get_verified_snapshot_block(state)

    # [한국어 요약] 아래 f-string 프롬프트는 LLM에게 다음을 지시합니다:
    # "너는 재종합자가 아니라 최종 '리스크 감독(risk-oversight) 게이트'다.
    # 리서치 매니저(RM)가 강세/약세 리서치 토론에서 이미 제안 등급을 정했다.
    # 네 임무는 리스크 관리 토론의 관점에서 그 등급을 확정(CONFIRM)할지,
    # 아니면 척도를 위/아래로 움직여 뒤집을지(OVERRIDE)를 판정하는 것이다.
    # [비대칭 override 기준 — CONFIRM이 기본값, override 시 자본 보호 우선]
    # (재판정에서 과교정이 확인되어 문턱을 2회 상향: 95%→60%→목표 15~45%.
    #  DOWNGRADE는 3개 관문(구체성·반박 생존·RM 미반영)을 모두 통과해야 하며,
    #  보수 분석가의 우려 제기 자체는 하향 근거가 아님 — RM이 이미
    #  반영한 통상적 밸류에이션/모멘텀/거시 경계는 하향 사유가 아님.)
    #   - CONFIRM(기본값): 리스크 토론이 RM이 이미 반영한 것 이상의 실질적
    #     리스크를 제기하지 않을 때. 연구 토론이 이미 가늠한 통상적 경계는 유지.
    #   - DOWNGRADE(Hold/Sell 쪽): 연구 단계가 진짜로 놓쳤거나 과소평가하고
    #     해소하지 못한, 리스크 조정 판단을 바꿀 만큼 구체적·중대한 하방 리스크가
    #     리스크 토론에서 드러날 때만 (집중/이벤트/유동성 리스크·펀더멘털 악화 등).
    #   - UPGRADE(드물어야 함): 연구 계획이 근거가 탄탄한 유리한 비대칭성 대비
    #     과도하게 보수적임을 리스크 토론이 보일 때만.
    # 최종적으로 RM 제안 등급 대비 CONFIRM/UPGRADE/DOWNGRADE 중 무엇인지 명시하고,
    # 등급을 움직인 경우 그것을 정당화하는 구체적 리스크-토론 근거를 인용하라.
    # 구체적 리스크 근거 없이 등급을 움직이지 말되, 고무도장도 되지 마라 —
    # 리스크 토론이 리스크 조정 그림을 실질적으로 바꾸면 그에 따라 행동하라.
    # 등급(Rating)은 Buy/Overweight/Hold/Underweight/Sell 중 정확히 하나.
    # [평가 루브릭 — 중기 로드맵 #3]·[편향검증 Phase 2 기저율 균형 문구]는 그대로
    # 보존한다: 리스크 토론 판정은 수사가 아닌 논거 품질(증거 접지·응답성·리스크
    # 비대칭)로 하고, 양/음 알파는 대략 반반이므로 낙관/행동 욕구가 아니라 증거가
    # 등급을 정하게 하며, 증거가 진정으로 균형이면 Hold도 정당하다.
    # [시계 정합 — 작업이력 21] 등급이 판단하는 지평을 holding_days 기반으로
    # 명시하고(get_horizon_instruction — RM·리플렉션과 동일 지평), override
    # 판단도 같은 지평 위에서 하라는 문장을 추가 — 지평 안에 작동할 수 없는
    # 리스크/기회는 맥락일 뿐 등급 이동 근거가 아니다.
    # 외부 도구는 사용하지 말라."
    # ※ 프롬프트를 번역하면 모델 출력 형식이 깨질 수 있어 영어 원문을 유지합니다.
    return f"""You are the final RISK-OVERSIGHT gate, not a re-synthesizer. The Research Manager has already set a proposed rating from the bull/bear research debate. Your task is to decide — through the lens of the RISK-management debate — whether to CONFIRM that rating or OVERRIDE it (move it up or down the scale).

{instrument_context}

---

**Rating Scale** (use exactly one):
- **Buy**: Strong conviction to enter or add to position
- **Overweight**: Favorable outlook, gradually increase exposure
- **Hold**: Maintain current position, no action needed
- **Underweight**: Cautious outlook, gradually reduce exposure
- **Sell**: Strong conviction to exit the position or avoid entry

**The Research Manager proposed: {rm_rating}** — this is your anchor, and confirming it is the default. The Research Manager already weighed the bull and bear cases and the ordinary valuation, momentum, and macro risks; keep their rating unless the RISK debate surfaces something they genuinely missed.

**Risk-Oversight Override Criteria** (asymmetric — when you do override, capital preservation comes first):
- **CONFIRM** (the default, and the majority outcome): when the risk debate raises no material risk beyond what the Research Manager already accounted for. Ordinary valuation, momentum, or macro caution that the research debate already weighed is NOT grounds to move — confirm.
- **DOWNGRADE** (toward Hold / Sell): only when a downside risk clears ALL THREE bars — (1) **specific and decision-relevant** (a concrete concentration / event / liquidity risk or severe fundamental deterioration, not a general restatement of caution), (2) **survived rebuttal** — the aggressive and neutral analysts failed to answer it in the risk debate, and (3) **not already priced** by the Research Manager's rating. The conservative analyst will always raise concerns — that is their role, and their mere presence is NOT grounds to downgrade.
- **UPGRADE** (toward Buy; rare): only when the risk debate shows the research plan was excessively cautious against a well-grounded favorable asymmetry.

State explicitly whether you CONFIRM, UPGRADE, or DOWNGRADE relative to the RM's proposed rating. In a well-functioning pipeline most ratings are CONFIRMED and overrides are the minority; when you do override, move by a single band unless the risk is severe. Overriding moves real money, so require a risk that clears all three bars above — but do not rubber-stamp either: when the risk debate genuinely changes the risk-adjusted picture, act on it.

**Context:**
- Research Manager's investment plan: **{research_plan}**
- Trader's transaction proposal: **{trader_plan}**
{lessons_line}
**Analyst Reports** (original evidence — cross-check the debaters' claims against these reports; a report may be empty if that analyst was not run):
Market Research Report: {market_research_report}
Social Media Sentiment Report: {sentiment_report}
Latest World Affairs Report: {news_report}
Company Fundamentals Report: {fundamentals_report}

{snapshot_block}**Risk Analysts Debate History:**
{history}

---

**Evaluation Rubric** (judge argument quality, not rhetoric — apply each criterion to every risk analyst):
1. **Evidence grounding**: Is each analyst's core claim backed by specific numbers or facts from the analyst reports above? Discount any claim you cannot trace back to a report.
2. **Responsiveness**: Did each analyst actually engage with the strongest opposing argument? An argument that was never answered still stands; a rebuttal that dodges the point does not count as an answer. Discount claims that were challenged and left unanswered.
3. **Risk asymmetry**: Weigh the magnitude of being wrong on each side — the downside if the aggressive view fails versus the opportunity cost if the cautious view fails — not merely the number of arguments raised.

Rate in proportion to the evidence and ground every conclusion in specific evidence from the analyst reports and the debate. Across many large-cap stock-days, positive and negative alpha are roughly equally common — do not let optimism or the urge to act set your rating. Hold is a legitimate finding when the evidence is genuinely balanced.

{get_horizon_instruction()} Judge override-worthiness on that same horizon: a risk (or opportunity) that cannot plausibly act within it is context, not grounds to move the rating.

{NO_EXTERNAL_TOOLS}{get_language_instruction()}"""


def create_portfolio_manager(llm):
    # 구조화 출력 바인딩: LLM이 PortfolioDecision 스키마 형태로 응답하도록 감쌉니다.
    structured_llm = bind_structured(llm, PortfolioDecision, "Portfolio Manager")

    # LangGraph 노드 함수: 상태(state) 딕셔너리를 입력받아
    # 갱신할 키들만 담은 딕셔너리를 반환합니다.
    def portfolio_manager_node(state) -> dict:
        # [NO_DATA 결정론적 게이트 — 중기 로드맵 #4] LLM 호출 전 최상단 가드:
        # 시장 분석가가 도구 결과의 NO_DATA 센티널을 감지해 내린 기계 판독
        # 플래그(market_data_ok=False)가 있으면, LLM 판정 없이 결정론적으로
        # Hold를 확정합니다. 조건부 엣지 추가 대신 노드 내부 가드를 택해
        # 그래프 흐름(setup.py)을 바꾸지 않습니다. 플래그가 없는 상태
        # (구형 체크포인트, 시장 분석가 미선택, 테스트 최소 상태)는 기본값
        # True로 기존 동작을 유지합니다.
        if not state.get("market_data_ok", True):
            risk_debate_state = state["risk_debate_state"]
            return {
                "risk_debate_state": {
                    **risk_debate_state,
                    "judge_decision": FORCED_HOLD_DECISION,
                    "latest_speaker": "Judge",
                },
                "final_trade_decision": FORCED_HOLD_DECISION,
            }

        risk_debate_state = state["risk_debate_state"]

        # 판정 프롬프트 구성은 모듈 함수로 분리되어 pm_probe와 공유됩니다.
        # (리스크 감독 게이트 재프레이밍 — BACKLOG.md B2 옵션 b)
        prompt = build_portfolio_manager_prompt(state)

        # 구조화 출력을 우선 시도하고, 지원하지 않는 제공자에서는
        # 자유 텍스트 생성으로 대체(fallback)하여 최종 결정 텍스트를 얻습니다.
        final_trade_decision = invoke_structured_or_freetext(
            structured_llm,
            llm,
            prompt,
            render_pm_decision,
            "Portfolio Manager",
            # PM의 출력은 시그널 파서와 메모리 태그가 소비하므로, 자유 텍스트
            # 폴백에서도 영어 등급 줄을 강제해 등급 추출을 보장한다.
            require_rating_line=True,
        )

        # 리스크 토론 상태(risk_debate_state)를 갱신합니다: 최종 결정을
        # "judge_decision"(판정 결과)에 기록하고, 기존 토론 이력은 그대로 보존하며,
        # 마지막 발언자를 "Judge"(판정자)로 표시합니다.
        # ※ dict 키 이름은 프로그램 동작에 쓰이므로 절대 변경하면 안 됩니다.
        new_risk_debate_state = {
            "judge_decision": final_trade_decision,
            "history": risk_debate_state["history"],
            "aggressive_history": risk_debate_state["aggressive_history"],
            "conservative_history": risk_debate_state["conservative_history"],
            "neutral_history": risk_debate_state["neutral_history"],
            "latest_speaker": "Judge",
            "current_aggressive_response": risk_debate_state["current_aggressive_response"],
            "current_conservative_response": risk_debate_state["current_conservative_response"],
            "current_neutral_response": risk_debate_state["current_neutral_response"],
            "count": risk_debate_state["count"],
        }

        # 상태(state) 갱신: 갱신된 리스크 토론 상태와 최종 거래 결정을 반환합니다.
        return {
            "risk_debate_state": new_risk_debate_state,
            "final_trade_decision": final_trade_decision,
        }

    return portfolio_manager_node
