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
    get_instrument_context_from_state,
    get_language_instruction,
)
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

        # 분석가 4종 원본 보고서: 프롬프트의 "분석가 보고서에 근거하라" 지시가
        # 실제로 이행 가능하도록 리스크 토론자와 동일한 방식으로 제공합니다.
        # 분석가 일부만 선택된 실행에서는 키가 없거나 빈 문자열일 수 있으므로
        # .get()으로 안전하게 꺼냅니다.
        market_research_report = state.get("market_report", "")
        sentiment_report = state.get("sentiment_report", "")
        news_report = state.get("news_report", "")
        fundamentals_report = state.get("fundamentals_report", "")

        # [한국어 요약] 아래 메시지들은 LLM에게 다음을 지시하는 프롬프트입니다:
        # - system: "당신은 시장 데이터를 분석해 투자 결정을 내리는 트레이딩 에이전트다.
        #   분석에 근거해 매수/매도/보유 중 하나의 구체적 추천을 제시하고,
        #   분석가 보고서와 리서치 계획에 근거를 두라. 외부 도구는 사용하지 말라."
        # - user: "분석가 팀의 종합 분석으로 만든 {company_name} 투자 계획이다.
        #   기술적 추세, 거시 지표, 소셜 미디어 감성이 반영되어 있다.
        #   아래에 계획의 근거가 된 분석가 원본 보고서 4종을 제공하니
        #   계획의 주장을 검증하고 누락된 신호를 확인하는 데 사용하라
        #   (해당 분석가가 실행되지 않았으면 보고서가 비어 있을 수 있다).
        #   이를 토대로 다음 거래 결정을 평가하라."
        # ※ 프롬프트를 번역하면 모델 출력 형식이 깨질 수 있어 영어 원문을 유지합니다.
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a trading agent analyzing market data to make investment decisions. "
                    "Based on your analysis, provide a specific recommendation to buy, sell, or hold. "
                    "Anchor your reasoning in the analysts' reports and the research plan. "
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
                    f"social media sentiment. Use this plan as a foundation for evaluating your next "
                    f"trading decision.\n\n"
                    f"Here are the original analyst reports the plan was built on. Use them to verify "
                    f"the plan's claims and to catch any signal it may have missed. A report may be "
                    f"empty if that analyst was not run; rely on the reports that are available.\n\n"
                    f"Market Research Report: {market_research_report}\n"
                    f"Social Media Sentiment Report: {sentiment_report}\n"
                    f"Latest World Affairs Report: {news_report}\n"
                    f"Company Fundamentals Report: {fundamentals_report}\n\n"
                    f"Proposed Investment Plan: {investment_plan}\n\n"
                    f"Leverage these insights to make an informed and strategic decision."
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
