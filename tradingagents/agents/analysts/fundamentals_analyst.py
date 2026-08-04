# ============================================================================
# 펀더멘털 분석가(Fundamentals Analyst) 모듈
#
# 이 에이전트는 기업의 재무제표(financial statements), 기업 개요, 재무 이력 등
# 펀더멘털(기초 체력) 정보를 분석하여 보고서를 작성하는 역할을 합니다.
# 전체 파이프라인(분석가 → 리서처 토론 → 트레이더 → 리스크 토론 → 포트폴리오 매니저)에서
# 가장 앞 단계인 "분석가" 팀의 한 명으로, 여기서 만든 보고서(fundamentals_report)는
# 이후 강세론자(Bull)/약세론자(Bear) 리서처 토론의 근거 자료로 사용됩니다.
# ============================================================================

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.utils.agent_utils import (
    get_balance_sheet,
    get_cashflow,
    get_fundamentals,
    get_income_statement,
    get_instrument_context_from_state,
    get_language_instruction,
)


def create_fundamentals_analyst(llm):
    # LangGraph 노드 함수: 그래프 실행 중 이 함수가 호출되며,
    # 현재 상태(state) 딕셔너리를 입력받아 갱신할 키들만 담은 딕셔너리를 반환합니다.
    def fundamentals_analyst_node(state):
        current_date = state["trade_date"]  # 분석 기준일(거래일)
        instrument_context = get_instrument_context_from_state(state)  # 분석 대상 종목/자산 정보 문자열

        # LLM이 호출할 수 있는 도구(tool) 목록:
        # 종합 펀더멘털, 재무상태표(balance sheet), 현금흐름표(cashflow), 손익계산서(income statement)
        tools = [
            get_fundamentals,
            get_balance_sheet,
            get_cashflow,
            get_income_statement,
        ]

        # [한국어 요약] 아래 system_message는 LLM에게 다음을 지시하는 프롬프트입니다:
        # "기업의 최근 보고된 재무와 기초 체력을 분석해, 주가 이면의 기업의 질과
        # 밸류에이션 맥락을 트레이더에게 제공하는 종합 보고서를 작성하라.
        # 분석 프레임 5개 축: (1) 최근 실적 vs 추세 — 매출·마진의 YoY/QoQ 방향,
        # 가이던스·전망 변화, (2) 어닝 품질 — FCF vs 순이익 괴리, 발생액 누적,
        # 매출보다 빠른 재고/매출채권 증가, 주식 희석, (3) 밸류에이션 맥락 —
        # 자기 역사·섹터 대비 상대 위치와 '현재 가격이 이미 가정하는 것',
        # (4) 대차대조표 리스크 — 레버리지·만기·이자보상·유동성, (5) 캘린더 —
        # 다음 실적 발표일 등 예정 이벤트와 거래일과의 근접도.
        # 접지(grounding) 규칙: 모든 수치에 회계기간 라벨(FY/분기/TTM)을 붙이고,
        # 도구 출력에 있는 숫자만 인용하며(없으면 추정하지 말고 없다고 명시),
        # 재무는 분기 단위로 지연되므로 각 재무제표가 거래일 대비 얼마나 오래된
        # 것인지 밝혀라. 끝에 핵심 요점 Markdown 표를 붙이고, 재무 도구들을
        # 활용하라." (작업이력 21 — 원본의 '지난 1주일 펀더멘털' 문구는 재무의
        # 분기 주기와 맞지 않아 교체, 분석 프레임·접지 규칙 신설)
        # ※ 프롬프트를 번역하면 모델 출력 형식이 깨질 수 있어 영어 원문을 유지합니다.
        # ※ 기존 코드는 문자열 끝 콤마로 system_message가 1-튜플이 되어
        #   프롬프트에 ("...",) 형태로 렌더링되던 버그가 있어 함께 수정.
        system_message = (
            "You are a fundamentals analyst researching a company's most recently reported financials and underlying quality. Write a comprehensive fundamentals report that gives traders the company-quality and valuation context behind the price. Structure your analysis around these five axes:\n"
            "1. Latest results vs. trend: revenue and margin direction (YoY and QoQ), and any change in guidance or outlook visible in the data.\n"
            "2. Earnings quality: free cash flow versus net income, accrual buildup, inventory or receivables growing faster than revenue, and share dilution.\n"
            "3. Valuation context: current multiples relative to the company's own history and its sector — state what the current price already assumes, not just whether a multiple looks high or low in isolation.\n"
            "4. Balance-sheet risk: leverage, debt maturities, interest coverage, and liquidity.\n"
            "5. Calendar: the next earnings date or other scheduled corporate events if visible in the tool output, and how close the trade date is to them.\n"
            "Grounding rules: label every figure with its fiscal period (e.g. FY2025 Q2, TTM); cite only numbers that appear in the tool output — if a figure is unavailable, say so rather than estimating it; financial statements are reported quarterly and lag the price, so note how stale each statement is relative to the trade date.\n"
            "Provide specific, actionable insights with supporting evidence to help traders make informed decisions."
            + " Make sure to append a Markdown table at the end of the report to organize key points in the report, organized and easy to read."
            + " Use the available tools: `get_fundamentals` for comprehensive company analysis, `get_balance_sheet`, `get_cashflow`, and `get_income_statement` for specific financial statements."
            + get_language_instruction()
        )

        # [한국어 요약] 아래 공통 시스템 프롬프트는 LLM에게 다음을 지시합니다:
        # "당신은 다른 어시스턴트들과 협업하는 AI다. 도구를 사용해 진행하고,
        # 완전히 답하지 못해도 괜찮다(다른 어시스턴트가 이어받는다).
        # 오늘 날짜({current_date})를 '현재'로 간주하라."
        # ※ 매수/매도 최종 제안 지시는 넣지 않습니다 — 분석가는 파이프라인
        #   1단계로 보고서만 작성하며, 최종 결정은 하류(트레이더·포트폴리오
        #   매니저)의 역할입니다.
        # {tool_names}, {system_message} 등은 아래 partial()로 채워지는 자리표시자입니다.
        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Use the provided tools to progress towards answering the question."
                    " If you are unable to fully answer, that's OK; another assistant with different tools"
                    " will help where you left off. Execute what you can to make progress."
                    " You have access to the following tools: {tool_names}."
                    " Today's date is {current_date}; treat it as 'now' for all analysis and tool-call date ranges. {instrument_context}\n"
                    "{system_message}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        # 프롬프트 템플릿의 자리표시자에 실제 값을 부분 적용(partial)합니다.
        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(tool_names=", ".join([tool.name for tool in tools]))
        prompt = prompt.partial(current_date=current_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        # 툴 바인딩: llm.bind_tools(tools)로 LLM이 위 도구들을 호출할 수 있게 연결하고,
        # 프롬프트와 파이프(|)로 이어 하나의 실행 체인(chain)을 만듭니다.
        chain = prompt | llm.bind_tools(tools)

        # 전용 채널(fundamentals_messages)에 쌓인 대화만 넣어 LLM을 실행합니다.
        # 분석가 병렬화(중기 로드맵 #6)로 다른 분석가의 대화와 섞이지 않습니다.
        result = chain.invoke(state["fundamentals_messages"])

        report = ""

        # 도구 호출(tool_calls)이 없다는 것은 LLM이 최종 보고서를 완성했다는 의미입니다.
        # (도구 호출이 있으면 그래프가 도구를 실행한 뒤 이 노드를 다시 방문합니다.)
        if len(result.tool_calls) == 0:
            report = result.content

        # 상태(state) 갱신: 전용 채널에 결과를 추가하고,
        # 완성된 보고서를 "fundamentals_report" 키에 저장해 후속 단계에서 사용하게 합니다.
        return {
            "fundamentals_messages": [result],
            "fundamentals_report": report,
        }

    return fundamentals_analyst_node
