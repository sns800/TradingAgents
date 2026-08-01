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
    get_instrument_context_from_state,
    get_language_instruction,
    get_verified_snapshot_block,
)
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

        instrument_context = get_instrument_context_from_state(state)  # 종목/자산 정보 문자열

        # 상태에서 리스크 토론 이력과 상위 단계 산출물들을 꺼냅니다.
        # 주의: 리스크 토론자들은 토큰 절약을 위해 압축 이력(debate_context
        # 참고)을 받지만, 심판인 이 노드는 판정 근거가 되므로 의도적으로 전체
        # 이력을 그대로 받습니다 — 여기에 condense_debate_history를 적용하지 마세요.
        history = state["risk_debate_state"]["history"]  # 리스크 분석가 토론 전체 이력
        risk_debate_state = state["risk_debate_state"]
        research_plan = state["investment_plan"]  # 리서치 매니저의 투자 계획
        trader_plan = state["trader_investment_plan"]  # 트레이더의 거래 제안

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
        # "포트폴리오 매니저로서 리스크 분석가들의 토론을 종합해 최종 거래 결정을 내려라.
        # 등급(Rating)은 Buy(매수)/Overweight(비중 확대)/Hold(보유)/
        # Underweight(비중 축소)/Sell(매도) 중 정확히 하나를 사용하라.
        # 리서치 매니저의 투자 계획, 트레이더의 거래 제안, (있다면) 과거 교훈,
        # 분석가 원본 보고서 4종, 리스크 토론 이력이 컨텍스트로 주어진다.
        # 토론자의 주장은 원본 보고서와 대조해 검증하라 (해당 분석가가
        # 실행되지 않았으면 보고서가 비어 있을 수 있다).
        # [평가 루브릭 — 중기 로드맵 #3] 리스크 토론의 판정은 수사가 아닌 논거
        # 품질로 하라: (1) 증거 접지 — 각 분석가의 핵심 주장이 보고서의 구체
        # 수치·사실로 뒷받침되는가(추적 불가한 주장은 할인), (2) 응답성 —
        # 상대의 최강 논거에 실제로 응답했는가(무응답 논거는 유효, 도전받고도
        # 무응답인 주장은 할인), (3) 리스크 비대칭 — 각 관점이 틀렸을 때의
        # 손실 크기를 가중하라.
        # [편향검증 Phase 2] 기존의 "단호하게 결정하라" 지시를 "증거에 비례해
        # 등급을 매겨라"로 교체 — 대형주의 수많은 종목-일 단위에서 양(+)의
        # 알파와 음(-)의 알파는 대략 비슷하게 흔하고, 증거가 진정으로 균형이면
        # Hold도 정당한 판정이다. 등급 척도도 대칭화: Sell에 Buy와 같은
        # "강한 확신(strong conviction)" 프레임을 부여하고, Underweight의
        # 이익 실현 문구(이익을 전제하는 프레임)를 중립 표현으로 수정.
        # 모든 결론은 분석가 보고서와 토론의 구체적 근거에 기반하라.
        # 외부 도구는 사용하지 말라."
        # ※ 프롬프트를 번역하면 모델 출력 형식이 깨질 수 있어 영어 원문을 유지합니다.
        prompt = f"""As the Portfolio Manager, synthesize the risk analysts' debate and deliver the final trading decision.

{instrument_context}

---

**Rating Scale** (use exactly one):
- **Buy**: Strong conviction to enter or add to position
- **Overweight**: Favorable outlook, gradually increase exposure
- **Hold**: Maintain current position, no action needed
- **Underweight**: Cautious outlook, gradually reduce exposure
- **Sell**: Strong conviction to exit the position or avoid entry

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

{NO_EXTERNAL_TOOLS}{get_language_instruction()}"""

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
