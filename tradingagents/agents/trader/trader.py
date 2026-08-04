# ============================================================================
# 트레이더(Trader) 모듈
#
# 이 에이전트는 리서치 매니저(Research Manager)가 강세론자(Bull)/약세론자(Bear)
# 토론을 정리해 만든 투자 계획(investment_plan)을 받아, 이를 구체적인
# 매수(Buy)/보유(Hold)/매도(Sell) 거래 제안으로 바꾸는 역할을 합니다.
# 전체 파이프라인(분석가 → 리서처 토론 → 트레이더 → 리스크 토론 → 포트폴리오 매니저)에서
# 중간 단계에 위치하며, 여기서 만든 거래 제안(trader_investment_plan)은
# 이후 리스크 분석가 토론과 포트폴리오 매니저의 최종 결정에 입력됩니다.
# ============================================================================

"""트레이더(Trader): 리서치 매니저의 투자 계획을 구체적인 거래 제안으로 변환합니다."""

from __future__ import annotations

import functools

from langchain_core.messages import AIMessage

from tradingagents.agents.schemas import TraderProposal, render_trader_proposal
from tradingagents.agents.utils.agent_utils import (
    get_horizon_instruction,
    get_instrument_context_from_state,
    get_language_instruction,
    get_verified_snapshot_block,
)
from tradingagents.agents.utils.rating import parse_rating
from tradingagents.agents.utils.structured import (
    NO_EXTERNAL_TOOLS,
    bind_structured,
    invoke_structured_or_freetext,
)


def create_trader(llm):
    # 구조화 출력 바인딩: LLM이 TraderProposal 스키마 형태로 응답하도록 감쌉니다.
    structured_llm = bind_structured(llm, TraderProposal, "Trader")

    # LangGraph 노드 함수: 상태(state) 딕셔너리를 입력받아
    # 갱신할 키들만 담은 딕셔너리를 반환합니다.
    def trader_node(state, name):
        company_name = state["company_of_interest"]  # 분석 대상 종목/기업
        instrument_context = get_instrument_context_from_state(state)  # 종목/자산 정보 문자열
        investment_plan = state["investment_plan"]  # 리서치 매니저가 작성한 투자 계획

        # [트레이더 역할 재정의 — 작업이력 22] RM 제안 등급을 결정론적으로 추출해
        # 명시적 앵커로 주입한다 (PM의 rm_anchor와 동일 패턴). B2 전수조사에서
        # RM→트레이더 "이견" 24/27이 5단계(RM)↔3단계(트레이더) 척도 차이
        # 아티팩트였다 — 등급이 계획 본문에 묻혀 있으면 트레이더가 무엇을
        # 실행으로 옮기는지 기준점을 인지하지 못한다.
        rm_rating = parse_rating(investment_plan, context="trader:rm_anchor")

        # 분석가 4종 원본 보고서: 프롬프트의 "분석가 보고서에 근거하라" 지시가
        # 실제로 이행 가능하도록 리스크 토론자와 동일한 방식으로 제공합니다.
        # 분석가 일부만 선택된 실행에서는 키가 없거나 빈 문자열일 수 있으므로
        # .get()으로 안전하게 꺼냅니다.
        market_research_report = state.get("market_report", "")
        sentiment_report = state.get("sentiment_report", "")
        news_report = state.get("news_report", "")
        fundamentals_report = state.get("fundamentals_report", "")

        # 검증 스냅샷 섹션(중기 로드맵 #5): 정확한 수치 인용의 기준점.
        # 비어 있으면 섹션 전체가 생략된다 (past_context 빈 값 가드와 동일 패턴).
        snapshot_block = get_verified_snapshot_block(state)

        # 과거 결정과 결과에서 얻은 교훈(메모리)이 있으면 실행 계획 수립용
        # 지시문과 함께 프롬프트에 포함합니다. 비어 있으면 섹션 전체를 생략해,
        # 빈 섹션이 존재하지 않는 과거 교훈을 지어내게(hallucinate) 유도하는 것을
        # 막는 #572 트레이드오프를 그대로 유지합니다 (PM과 동일한 패턴).
        past_context = state.get("past_context", "")
        # [한국어 요약] 아래 lessons 블록은 LLM에게 다음을 지시합니다:
        # "이미 결과가 확정된 과거 결정들의 교훈이다. 실행 계획(진입/청산 규율,
        # 포지션 크기, 리스크 통제)을 세울 때 참고해 과거에 지적된 실행 실수를
        # 반복하지 말라. 과거 등급이 이번 추천을 좌우하게 하지는 말라."
        lessons_block = (
            "Lessons from past decisions and their outcomes (reflections from "
            "already-resolved calls — apply them when constructing the execution plan: "
            "entry/exit discipline, position sizing, and risk controls; avoid repeating "
            "the execution mistakes flagged below, and do not let past ratings dictate "
            f"your new recommendation):\n{past_context}\n\n"
            if past_context
            else ""
        )

        # [한국어 요약] 아래 메시지들은 LLM에게 다음을 지시하는 프롬프트입니다:
        # - system: [트레이더 역할 재정의 — 작업이력 22] "너는 이 데스크의 실행
        #   트레이더다. 방향 등급은 리서치 매니저(RM)가 이미 정했다 — 방향을
        #   재판정하지 말고, 계획을 실행 가능한 거래로 바꾸는 것이 네 역할이다:
        #   RM 등급을 거래 방향으로 번역하고(Buy/Overweight→Buy, Hold→Hold,
        #   Underweight/Sell→Sell), 진입가·손절·포지션 크기와 실행 수준의 우려
        #   (유동성, 예정 이벤트 근접, 갭 리스크)를 산출하라. 계획이 등급대로
        #   실행 불가능해 보이면 — 예: 보고서가 계획이 의존하는 레벨과 모순 —
        #   번역된 방향은 유지하되 그 우려를 reasoning에 명시하라(방향 이탈은
        #   B2 조사에서 PM이 무시하는 척도 노이즈만 만들었다). 분석가 보고서와
        #   리서치 계획에 근거를 두라. 외부 도구는 사용하지 말라."
        #   [시계 정합 — 작업이력 21] 평가 지평(holding_days) 지시문 + 진입가·
        #   손절·사이징이 그 지평 안에서 실행 가능하도록 설계하라는 문장 유지.
        # - user: "RM의 제안 등급은 {rm_rating}이다 — 이것이 네가 실행으로 옮길
        #   방향이다. 아래에 계획의 근거가 된 분석가 원본 보고서 4종을 제공하니
        #   실행 레벨(진입/손절)의 근거 수치를 찾고 실행 리스크를 확인하는 데
        #   사용하라 (해당 분석가가 실행되지 않았으면 보고서가 비어 있을 수 있다).
        #   (있다면) 과거 결정의 교훈이 이어지며, 실행 계획 수립 시 참고하라."
        # ※ 프롬프트를 번역하면 모델 출력 형식이 깨질 수 있어 영어 원문을 유지합니다.
        messages = [
            {
                "role": "system",
                "content": (
                    "You are the execution trader for this desk. The Research Manager has already "
                    "set the directional rating from the bull/bear research debate — your job is "
                    "not to re-litigate that direction, but to turn the plan into an executable "
                    "transaction. Translate the rating into the transaction direction "
                    "(Buy/Overweight → Buy; Hold → Hold; Underweight/Sell → Sell), then add the "
                    "execution value only you provide: a concrete entry price, stop-loss, and "
                    "position size, plus any execution-level concerns such as liquidity, gap risk, "
                    "or timing around scheduled events. If you believe the plan cannot be executed "
                    "as rated — for example the analyst reports contradict a level the plan depends "
                    "on — keep the translated direction but flag that concern explicitly in your "
                    "reasoning. Anchor every level and concern in the analysts' reports and the "
                    "research plan. "
                    + get_horizon_instruction()
                    + " Design the entry price, stop-loss, and position sizing so the plan is "
                    "executable within that horizon. "
                    + NO_EXTERNAL_TOOLS
                    + get_language_instruction()
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Based on a comprehensive analysis by a team of analysts, here is an investment "
                    f"plan tailored for {company_name}. {instrument_context} This plan incorporates "
                    f"insights from current technical market trends, macroeconomic indicators, and "
                    f"social media sentiment.\n\n"
                    f"**The Research Manager's proposed rating: {rm_rating}** — this is the "
                    f"direction you are translating into an executable transaction.\n\n"
                    f"Here are the original analyst reports the plan was built on. Use them to "
                    f"ground your execution levels (entry, stop) in concrete numbers and to spot "
                    f"execution risks the plan may have missed. A report may be empty if that "
                    f"analyst was not run; rely on the reports that are available.\n\n"
                    f"Market Research Report: {market_research_report}\n"
                    f"Social Media Sentiment Report: {sentiment_report}\n"
                    f"Latest World Affairs Report: {news_report}\n"
                    f"Company Fundamentals Report: {fundamentals_report}\n\n"
                    f"{snapshot_block}"
                    f"{lessons_block}"
                    f"Proposed Investment Plan: {investment_plan}\n\n"
                    f"Turn this plan into the executable transaction proposal."
                ),
            },
        ]

        # 구조화 출력을 우선 시도하고, 지원하지 않는 제공자(provider)에서는
        # 자유 텍스트 생성으로 대체(fallback)하여 거래 제안 텍스트를 얻습니다.
        trader_plan = invoke_structured_or_freetext(
            structured_llm,
            llm,
            messages,
            render_trader_proposal,
            "Trader",
        )

        # 상태(state) 갱신: 대화 메시지에 결과를 추가하고, 거래 제안을
        # "trader_investment_plan" 키에 저장해 리스크 토론 단계에서 사용하게 합니다.
        return {
            "messages": [AIMessage(content=trader_plan)],
            "trader_investment_plan": trader_plan,
            "sender": name,
        }

    # functools.partial로 name 인자를 "Trader"로 고정한 노드 함수를 반환합니다.
    return functools.partial(trader_node, name="Trader")
