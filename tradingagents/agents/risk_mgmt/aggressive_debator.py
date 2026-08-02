# =============================================================================
# [모듈 개요 - 초보자용]
# 공격적 리스크 애널리스트(Aggressive Risk Analyst) 에이전트입니다. 트레이더의
# 매매 결정을 놓고 "고위험·고수익 기회를 놓치지 말자"는 입장에서 보수적/중립적
# 애널리스트의 신중론을 반박합니다. 전체 파이프라인 「분석가 → 리서처 토론 →
# 트레이더 → 리스크 토론 → 포트폴리오 매니저」 중 네 번째 단계인 리스크 토론에서
# 공격적(Aggressive) 관점을 담당하며, 3자 토론 결과는 포트폴리오 매니저가
# 최종 판단에 활용합니다.
# =============================================================================

from tradingagents.agents.utils.agent_utils import (
    get_instrument_context_from_state,
    get_language_instruction,
    get_verified_snapshot_block,
)
from tradingagents.agents.utils.debate_context import condense_debate_history


def create_aggressive_debator(llm):
    # LangGraph 그래프에 노드(node)로 등록될 함수를 만들어 돌려주는 팩토리입니다.
    # 반환된 aggressive_node는 현재 상태(state) dict를 받아, 갱신할 키만 담은
    # dict를 돌려주고 LangGraph가 이를 기존 상태에 병합(merge)합니다.
    def aggressive_node(state) -> dict:
        # risk_debate_state: 공격적/보수적/중립적 3자 리스크 토론의 진행 상황을
        # 담는 하위 상태. history는 전체 토론 기록, aggressive_history는
        # 공격적 애널리스트 발언만 모은 기록입니다.
        risk_debate_state = state["risk_debate_state"]
        history = risk_debate_state.get("history", "")
        # 프롬프트용 압축 이력: 직전 발언은 전문, 그 이전 발언들은 각 300자
        # 절단 (토큰 O(라운드²) 완화 — 중기 로드맵 #3). 상태에 저장되는
        # history 원본은 그대로 유지되며, 심판(PM)은 전체 이력을 받는다.
        condensed_history = condense_debate_history(history)
        aggressive_history = risk_debate_state.get("aggressive_history", "")

        # 리서처 토론과 달리 이 두 주입은 중기 로드맵 #6(이중 주입 제거)에서
        # 의도적으로 유지합니다: 압축 이력(condense_debate_history)에는 직전
        # 발언 하나만 전문으로 남고 그 이전 발언(다른 한 상대의 최신 발언)은
        # 300자로 절단되므로, 3자 토론에서 두 상대의 최신 발언 전문 주입은
        # 순수한 중복이 아닙니다.
        # 반박 대상인 다른 두 애널리스트(보수적/중립적)의 최근 발언입니다.
        current_conservative_response = risk_debate_state.get("current_conservative_response", "")
        current_neutral_response = risk_debate_state.get("current_neutral_response", "")

        market_research_report = state["market_report"]
        sentiment_report = state["sentiment_report"]
        news_report = state["news_report"]
        fundamentals_report = state["fundamentals_report"]
        instrument_context = get_instrument_context_from_state(state)
        # 검증 스냅샷 섹션(중기 로드맵 #5): 정확한 수치 인용의 기준점.
        # 비어 있으면 섹션 전체가 생략된다 (past_context 빈 값 가드와 동일 패턴).
        snapshot_block = get_verified_snapshot_block(state)

        # 이전 단계에서 트레이더(Trader)가 내놓은 매매 결정. 이 토론의 심사 대상입니다.
        trader_decision = state["trader_investment_plan"]

        # [프롬프트 요약 - 한국어] 공격적 리스크 애널리스트 역할 지시문:
        # 증거의 가장 공격적인(고수익 추구) 해석을 옹호하되, 트레이더의 계획이
        # 증거 대비 위험을 과소·과대하게 지고 있으면 그 방향으로 비판하고,
        # 보수적/중립적 애널리스트의 각 논점에 데이터 기반으로 반박하며,
        # 고위험 접근이 최선인 이유를 대화체(서식 없이)로 설득하라는 내용.
        # 아래에 4종 분석 보고서와 토론 이력을 근거 자료로 제공합니다.
        # [편향검증 Phase 2] 기존 "트레이더의 결정을 옹호하는 설득력 있는
        # 논거를 만들라" 문구는 트레이더 결정 방향과 무관하게 무조건 옹호하는
        # 에코 루프(echo loop)를 만들므로, 결정이 아니라 '증거의 공격적 해석'을
        # 옹호하는 역할로 교체 — 계획이 과소 위험이면 더 대담하게, 과대
        # 위험이면 그 지점을 비판하게 한다.
        # [편향검증 b' — 리스크 토론 대칭화] 보수 분석가가 항상 구체적 하방을
        # 생산하는데 공격 반박이 "기회를 놓친다"式 일반론에 그치면, 심판(PM)이
        # 공격 논거를 수사로 discount하고 하향으로 기운다(직전 실험 override
        # 22/22 전부 하향). 이를 막기 위해 (1) 보수/중립 논거에 일반론이 아니라
        # 구체적 반증(수치·사실·메커니즘)으로 조목조목 반박하도록, (2) 상방 논거
        # 자체도 하방에 요구하는 것과 동일한 정량 근거를 갖추도록 강화한다.
        # (LLM 프롬프트이므로 영어 원문 유지)
        prompt = f"""As the Aggressive Risk Analyst, your role is to actively champion high-reward, high-risk opportunities, emphasizing bold strategies and competitive advantages. When evaluating the trader's decision or plan, focus intently on the potential upside, growth potential, and innovative benefits—even when these come with elevated risk. Ground your upside case in quantitative evidence from the reports — specific numbers, facts, or mechanisms — held to the same standard of proof you demand of the downside case; an appeal to "opportunity" or "risk-taking" that cites no evidence is not an argument. Here is the trader's decision:

{trader_decision}

Your task is to champion the most aggressive, highest-reward reading of the evidence — not the trader's decision itself. If the trader's plan takes on less risk than the evidence justifies, argue for the bolder position; if the plan overreaches beyond what the evidence supports, criticize it in that direction and point to where the real opportunity lies. Question and critique the conservative and neutral stances to demonstrate why your high-reward perspective offers the best path forward. Incorporate insights from the following sources into your arguments:

{instrument_context}
Market Research Report: {market_research_report}
Social Media Sentiment Report: {sentiment_report}
Latest World Affairs Report: {news_report}
Company Fundamentals Report: {fundamentals_report}
{snapshot_block}Here is the current conversation history (earlier arguments are truncated for brevity; the latest argument is shown in full): {condensed_history} Here are the last arguments from the conservative analyst: {current_conservative_response} Here are the last arguments from the neutral analyst: {current_neutral_response}. If there are no responses from the other viewpoints yet, present your own argument based on the available data.

Engage actively by answering each specific concern the conservative and neutral analysts raise with specific counter-evidence — name the number, fact, or mechanism that defuses each concrete risk, because a general appeal to opportunity does not rebut a specific downside. Where a concern is genuinely material and unpriced, say so plainly rather than dismissing it; where it is overstated or already reflected in the price, show precisely why with the data. Maintain a focus on debating and persuading, not just presenting data. Output conversationally as if you are speaking without any special formatting.""" + get_language_instruction()

        # LLM을 한 번 호출해 공격적 애널리스트의 발언을 생성합니다.
        response = llm.invoke(prompt)

        # 발언 앞에 화자 라벨을 붙입니다. (토론 기록에서 누구 발언인지 구분용)
        argument = f"Aggressive Analyst: {response.content}"

        # 토론 상태를 새로 만들어 돌려줍니다. history와 aggressive_history에는
        # 이번 발언을 덧붙이고, latest_speaker와 current_aggressive_response를
        # 갱신해 다음 화자가 이 발언에 반박하게 하며, 다른 두 애널리스트의 기록은
        # 그대로 보존합니다. count(발언 횟수)는 토론 종료 조건 판단에 쓰입니다.
        new_risk_debate_state = {
            "history": history + "\n" + argument,
            "aggressive_history": aggressive_history + "\n" + argument,
            "conservative_history": risk_debate_state.get("conservative_history", ""),
            "neutral_history": risk_debate_state.get("neutral_history", ""),
            "latest_speaker": "Aggressive",
            "current_aggressive_response": argument,
            "current_conservative_response": risk_debate_state.get("current_conservative_response", ""),
            "current_neutral_response": risk_debate_state.get(
                "current_neutral_response", ""
            ),
            "count": risk_debate_state["count"] + 1,
        }

        # LangGraph 규칙: 갱신하려는 상태 키만 담은 dict를 반환하면
        # 프레임워크가 전체 상태에 병합해 줍니다.
        return {"risk_debate_state": new_risk_debate_state}

    return aggressive_node
