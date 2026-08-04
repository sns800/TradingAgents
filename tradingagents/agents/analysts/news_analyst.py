# ============================================================================
# 뉴스 분석가(News Analyst) 모듈
#
# 이 에이전트는 최근 1주일간의 종목별 뉴스, 글로벌 거시경제(macroeconomics) 뉴스,
# FRED 거시 지표, 예측 시장(prediction market) 확률을 종합해
# 트레이딩과 거시경제 관점의 뉴스 보고서를 작성하는 역할을 합니다.
# 전체 파이프라인(분석가 → 리서처 토론 → 트레이더 → 리스크 토론 → 포트폴리오 매니저)에서
# 가장 앞 단계인 "분석가" 팀에 속하며, 여기서 만든 보고서(news_report)는
# 이후 강세론자(Bull)/약세론자(Bear) 토론의 근거 자료로 사용됩니다.
# ============================================================================

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.utils.agent_utils import (
    get_global_news,
    get_instrument_context_from_state,
    get_language_instruction,
    get_macro_indicators,
    get_news,
    get_prediction_markets,
)


def create_news_analyst(llm):
    # LangGraph 노드 함수: 상태(state) 딕셔너리를 입력받아
    # 갱신할 키들만 담은 딕셔너리를 반환합니다.
    def news_analyst_node(state):
        current_date = state["trade_date"]  # 분석 기준일(거래일)
        # 자산 유형에 따라 프롬프트 문구를 "company"(주식) 또는 "asset"(그 외)으로 조정
        asset_type = state.get("asset_type", "stock")
        asset_label = "company" if asset_type == "stock" else "asset"
        instrument_context = get_instrument_context_from_state(state)  # 분석 대상 종목/자산 정보 문자열

        # LLM이 호출할 수 있는 도구(tool) 목록:
        # 종목 뉴스, 글로벌 거시 뉴스, 거시경제 지표(FRED), 예측 시장 확률
        tools = [
            get_news,
            get_global_news,
            get_macro_indicators,
            get_prediction_markets,
        ]

        # [한국어 요약] 아래 system_message는 LLM에게 다음을 지시하는 프롬프트입니다:
        # "지난 1주일간의 뉴스와 트렌드를 분석해 트레이딩·거시경제 관점에서
        # 세계 현황 종합 보고서를 작성하라. 도구 사용법: get_news(종목별 뉴스),
        # get_global_news(광범위한 거시 뉴스), get_macro_indicators(FRED 실제 데이터로
        # 거시 논평의 근거 확보 — 예: CPI, 실업률, 기준금리, 10년물 국채, 수익률 곡선),
        # get_prediction_markets(미래 이벤트의 시장 내재 확률 — 예: 연준 금리 인하, 경기 침체).
        # 근거가 있는 실행 가능한 인사이트를 제공하고, 끝에 핵심 요점 Markdown 표를 붙여라."
        # [예정 이벤트 섹션 — 작업이력 22] "향후 1~2주 예정 이벤트(실적 발표일,
        # 중앙은행 회의, CPI·고용 등 주요 지표 발표, 섹터 이벤트)를 날짜와 함께
        # 'Upcoming events' 전용 섹션으로 정리하라 — 이벤트 직전 거래는 한산한
        # 캘린더와 리스크 프로파일이 다르므로 짧아도 중요하다. 도구 출력에
        # 예정 이벤트가 안 보이면 추측하지 말고 그렇다고 명시하라."
        # ※ 프롬프트를 번역하면 모델 출력 형식이 깨질 수 있어 영어 원문을 유지합니다.
        system_message = (
            f"You are a news researcher tasked with analyzing recent news and trends over the past week. Please write a comprehensive report of the current state of the world that is relevant for trading and macroeconomics. Use the available tools: get_news(ticker, start_date, end_date) for {asset_label}-specific news by ticker symbol, get_global_news(curr_date, look_back_days, limit) for broader macroeconomic news, get_macro_indicators(indicator, curr_date, look_back_days) to ground macro commentary in actual data from FRED (e.g. 'cpi', 'core_pce', 'unemployment', 'fed_funds_rate', '10y_treasury', 'yield_curve'), and get_prediction_markets(topic, limit) for live market-implied probabilities of forward-looking events (e.g. 'Fed rate cut', 'recession 2026', geopolitical or sector events). Provide specific, actionable insights with supporting evidence to help traders make informed decisions."
            + f" Include a dedicated 'Upcoming events' section: scheduled events over the next one to two weeks that could move the {asset_label} or the broader market — earnings dates, central-bank meetings, major macro releases (CPI, jobs), sector events — with their dates, drawing on the news and prediction-market output. Trading into a scheduled event carries a different risk profile than a quiet calendar, so this section matters even when it is short. If the tool output does not reveal any scheduled events, state that explicitly rather than guessing."
            + """ Make sure to append a Markdown table at the end of the report to organize key points in the report, organized and easy to read."""
            + get_language_instruction()
        )

        # [한국어 요약] 아래 공통 시스템 프롬프트는 LLM에게 다음을 지시합니다:
        # "너는 이 보고서의 유일한 책임 분석가다 — 하류 에이전트들이 이 보고서에
        # 의존하며, 네가 남긴 공백을 채워줄 다른 에이전트는 없다. 도구로 필요한
        # 데이터를 확보해 완결된 보고서까지 끌고 가라.
        # 오늘 날짜({current_date})를 '현재'로 간주하라."
        # [협업 프레임 잔재 제거 — 작업이력 22] 원본의 "완전히 답하지 못해도
        # 괜찮다, 다른 어시스턴트가 이어받는다"는 스웜 구조 잔재로, 전용 채널
        # 구조에서는 미완성 보고서의 명분이 되어 교체.
        # ※ 매수/매도 최종 제안 지시는 넣지 않습니다 — 분석가는 파이프라인
        #   1단계로 보고서만 작성하며, 최종 결정은 하류(트레이더·포트폴리오
        #   매니저)의 역할입니다.
        # {tool_names}, {system_message} 등은 아래 partial()로 채워지는 자리표시자입니다.
        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are the analyst solely responsible for this report in a multi-agent"
                    " trading pipeline; downstream agents rely on it and no other agent will"
                    " fill the gaps you leave. Use the provided tools to gather the data you"
                    " need and carry the analysis through to a complete report."
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
        # 프롬프트와 파이프(|)로 이어 하나의 실행 체인(chain)을 만든 뒤 실행합니다.
        # 전용 채널(news_messages)만 읽고 씁니다 — 분석가 병렬화(중기 로드맵
        # #6)로 다른 분석가의 대화와 섞이지 않습니다.
        chain = prompt | llm.bind_tools(tools)
        result = chain.invoke(state["news_messages"])

        report = ""

        # 도구 호출(tool_calls)이 없다는 것은 LLM이 최종 보고서를 완성했다는 의미입니다.
        # (도구 호출이 있으면 그래프가 도구를 실행한 뒤 이 노드를 다시 방문합니다.)
        if len(result.tool_calls) == 0:
            report = result.content

        # 상태(state) 갱신: 전용 채널에 결과를 추가하고,
        # 완성된 보고서를 "news_report" 키에 저장해 후속 단계에서 사용하게 합니다.
        return {
            "news_messages": [result],
            "news_report": report,
        }

    return news_analyst_node
