# ============================================================================
# 시장 분석가(Market Analyst) 모듈
#
# 이 에이전트는 주가 데이터와 기술적 지표(technical indicator) — 이동평균(SMA/EMA),
# MACD, RSI, 볼린저 밴드(Bollinger Bands), ATR, VWMA 등 — 를 해석하여
# 시장의 추세와 모멘텀을 분석한 보고서를 작성하는 역할을 합니다.
# 전체 파이프라인(분석가 → 리서처 토론 → 트레이더 → 리스크 토론 → 포트폴리오 매니저)에서
# 가장 앞 단계인 "분석가" 팀에 속하며, 여기서 만든 보고서(market_report)는
# 이후 강세론자(Bull)/약세론자(Bear) 토론과 트레이더 판단의 기술적 근거가 됩니다.
# ============================================================================

from langchain_core.messages import ToolMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.utils.agent_utils import (
    get_indicators,
    get_instrument_context_from_state,
    get_language_instruction,
    get_stock_data,
    get_verified_market_snapshot,
)
from tradingagents.agents.utils.market_data_validation_tools import (
    VERIFIED_SNAPSHOT_HEADER_PREFIX,
)
from tradingagents.dataflows.interface import is_no_data_sentinel


def _market_data_ok(messages) -> bool:
    """시장 도구 결과에 NO_DATA 센티널이 없으면 True를 반환한다 (결정론적 검사).

    [NO_DATA 결정론적 게이트 — 설계분석 중기 로드맵 #4] 시장 분석가의 전용
    메시지 채널(market_messages, 중기 #6에서 분리)에 쌓인 도구 결과
    (ToolMessage)를 훑어, 벤더 라우터나 검증 스냅샷 도구가 반환한 NO_DATA
    센티널이 하나라도 있으면 핵심 시장 데이터가 확보되지 않은 것으로
    판정합니다. 전용 채널에는 시장 도구의 결과만 쌓이므로 다른 분석가의
    도구 출력이 판정을 오염시키지 않습니다. LLM 판단이 아니라 문자열
    검사만 사용하므로 결과가 결정론적입니다. 이 플래그는 포트폴리오
    매니저가 LLM 호출 없이 강제 Hold로 분기하는 근거가 됩니다.
    """
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        content = message.content
        text = content if isinstance(content, str) else str(content)
        if is_no_data_sentinel(text):
            return False
    return True


def _extract_verified_snapshot(messages) -> str:
    """도구 메시지에서 검증 스냅샷(get_verified_market_snapshot 출력)을 찾아 반환한다.

    [검증 스냅샷 보존 — 설계분석 중기 로드맵 #5] 하류(토론자·트레이더·PM)는
    시장 분석가의 전용 메시지 채널(market_messages)을 읽지 않으므로, 수치
    인용의 기준점으로 쓸 수 있도록 스냅샷 원문을 별도 상태 필드로 옮기기
    위한 결정론적 추출입니다. 식별 기준은 두 가지입니다:
    (1) ToolNode가 채우는 ToolMessage.name == "get_verified_market_snapshot",
    (2) name이 없는 환경(일부 프레임워크 버전·테스트)을 위한 폴백으로
        스냅샷 렌더러의 고정 헤더 접두사.
    스냅샷 도구가 여러 번 호출됐으면 마지막 출력이 최신이므로 그것을
    사용하고, NO_DATA 센티널이면(데이터 부재) 빈 문자열을 반환합니다.
    """
    snapshot = ""
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        content = message.content
        text = content if isinstance(content, str) else str(content)
        is_snapshot = (
            getattr(message, "name", None) == "get_verified_market_snapshot"
            or text.lstrip().startswith(VERIFIED_SNAPSHOT_HEADER_PREFIX)
        )
        if is_snapshot:
            snapshot = "" if is_no_data_sentinel(text) else text
    return snapshot


def create_market_analyst(llm):

    # LangGraph 노드 함수: 상태(state) 딕셔너리를 입력받아
    # 갱신할 키들만 담은 딕셔너리를 반환합니다.
    def market_analyst_node(state):
        current_date = state["trade_date"]  # 분석 기준일(거래일)
        instrument_context = get_instrument_context_from_state(state)  # 분석 대상 종목/자산 정보 문자열

        # LLM이 호출할 수 있는 도구(tool) 목록:
        # 주가 데이터 조회, 기술적 지표 계산, 검증된 시장 스냅샷 조회
        tools = [
            get_stock_data,
            get_indicators,
            get_verified_market_snapshot,
        ]

        # [한국어 요약] 아래 system_message는 LLM에게 다음을 지시하는 프롬프트입니다:
        # "주어진 시장 상황/전략에 가장 적합한 기술적 지표를 최대 8개까지,
        # 중복 없이 상호 보완적으로 선택하라. 지표 카테고리는
        # 이동평균(50/200 SMA, 10 EMA), MACD 계열, 모멘텀(RSI),
        # 변동성(볼린저 밴드, ATR), 거래량 기반(VWMA)이며 각 지표의 용도와 주의점이 명시됨.
        # 도구 호출 시 반드시 위에 정의된 정확한 지표 이름을 사용하고,
        # get_stock_data를 먼저 호출해 CSV를 확보한 뒤 get_indicators를 사용하라.
        # 최종 보고서 작성 전 get_verified_market_snapshot을 호출해 이를
        # 정확한 가격/지표 수치의 유일한 근거(source of truth)로 삼고,
        # 다른 도구 출력과 충돌하면 임의로 숫자를 만들지 말고 불일치를 명시하라.
        # 매우 상세하고 섬세한 추세 보고서를 쓰고, 끝에 핵심 요점 Markdown 표를 붙여라."
        # ※ 프롬프트를 번역하면 모델 출력 형식이 깨질 수 있어 영어 원문을 유지합니다.
        system_message = (
            """You are a trading assistant tasked with analyzing financial markets. Your role is to select the **most relevant indicators** for a given market condition or trading strategy from the following list. The goal is to choose up to **8 indicators** that provide complementary insights without redundancy. Categories and each category's indicators are:

Moving Averages:
- close_50_sma: 50 SMA: A medium-term trend indicator. Usage: Identify trend direction and serve as dynamic support/resistance. Tips: It lags price; combine with faster indicators for timely signals.
- close_200_sma: 200 SMA: A long-term trend benchmark. Usage: Confirm overall market trend and identify golden/death cross setups. Tips: It reacts slowly; best for strategic trend confirmation rather than frequent trading entries.
- close_10_ema: 10 EMA: A responsive short-term average. Usage: Capture quick shifts in momentum and potential entry points. Tips: Prone to noise in choppy markets; use alongside longer averages for filtering false signals.

MACD Related:
- macd: MACD: Computes momentum via differences of EMAs. Usage: Look for crossovers and divergence as signals of trend changes. Tips: Confirm with other indicators in low-volatility or sideways markets.
- macds: MACD Signal: An EMA smoothing of the MACD line. Usage: Use crossovers with the MACD line to trigger trades. Tips: Should be part of a broader strategy to avoid false positives.
- macdh: MACD Histogram: Shows the gap between the MACD line and its signal. Usage: Visualize momentum strength and spot divergence early. Tips: Can be volatile; complement with additional filters in fast-moving markets.

Momentum Indicators:
- rsi: RSI: Measures momentum to flag overbought/oversold conditions. Usage: Apply 70/30 thresholds and watch for divergence to signal reversals. Tips: In strong trends, RSI may remain extreme; always cross-check with trend analysis.

Volatility Indicators:
- boll: Bollinger Middle: A 20 SMA serving as the basis for Bollinger Bands. Usage: Acts as a dynamic benchmark for price movement. Tips: Combine with the upper and lower bands to effectively spot breakouts or reversals.
- boll_ub: Bollinger Upper Band: Typically 2 standard deviations above the middle line. Usage: Signals potential overbought conditions and breakout zones. Tips: Confirm signals with other tools; prices may ride the band in strong trends.
- boll_lb: Bollinger Lower Band: Typically 2 standard deviations below the middle line. Usage: Indicates potential oversold conditions. Tips: Use additional analysis to avoid false reversal signals.
- atr: ATR: Averages true range to measure volatility. Usage: Set stop-loss levels and adjust position sizes based on current market volatility. Tips: It's a reactive measure, so use it as part of a broader risk management strategy.

Volume-Based Indicators:
- vwma: VWMA: A moving average weighted by volume. Usage: Confirm trends by integrating price action with volume data. Tips: Watch for skewed results from volume spikes; use in combination with other volume analyses.

- Select indicators that provide diverse and complementary information. Avoid redundancy (e.g., do not select both rsi and stochrsi). Also briefly explain why they are suitable for the given market context. When you tool call, please use the exact name of the indicators provided above as they are defined parameters, otherwise your call will fail. Please make sure to call get_stock_data first to retrieve the CSV that is needed to generate indicators. Then use get_indicators with the specific indicator names.

Before writing the final report, call get_verified_market_snapshot for this ticker and the current date, and treat it as the source of truth for any exact OHLCV, price-level, or indicator-value claim. If another tool's output conflicts with the verified snapshot, flag the discrepancy rather than inventing a reconciled number. Do not claim historical validation, support/resistance bounces, or exact percentage moves unless they are directly supported by tool output with concrete dates and prices.

Write a very detailed and nuanced report of the trends you observe. Provide specific, actionable insights with supporting evidence to help traders make informed decisions."""
            + """ Make sure to append a Markdown table at the end of the report to organize key points in the report, organized and easy to read."""
            + get_language_instruction()
        )

        # [한국어 요약] 아래 공통 시스템 프롬프트는 LLM에게 다음을 지시합니다:
        # "당신은 다른 어시스턴트들과 협업하는 AI다. 도구를 사용해 진행하고,
        # 완전히 답하지 못해도 괜찮다(다른 어시스턴트가 이어받는다).
        # 오늘 날짜({current_date})를 '현재'로 간주하라."
        # ※ 매수/매도 최종 제안 지시는 넣지 않습니다 — 분석가는 파이프라인
        #   1단계로 보고서만 작성하며, 최종 결정은 하류(트레이더·포트폴리오
        #   매니저)의 역할입니다. 최상류에서 결론을 박으면 하류 5단계 등급
        #   체계와 충돌하고 토론을 앵커링합니다.
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

        # 전용 채널(market_messages)에 쌓인 대화만 넣어 LLM을 실행합니다.
        # 공유 messages 채널 대신 전용 채널을 쓰는 것이 분석가 병렬화의
        # 핵심입니다 (중기 로드맵 #6) — 다른 분석가의 대화와 섞이지 않습니다.
        result = chain.invoke(state["market_messages"])

        report = ""

        # 도구 호출(tool_calls)이 없다는 것은 LLM이 최종 보고서를 완성했다는 의미입니다.
        # (도구 호출이 있으면 그래프가 도구를 실행한 뒤 이 노드를 다시 방문합니다.)
        if len(result.tool_calls) == 0:
            report = result.content

        # 상태(state) 갱신: 전용 채널에 결과를 추가하고,
        # 완성된 보고서를 "market_report" 키에 저장해 후속 단계에서 사용하게 합니다.
        # market_data_ok와 verified_snapshot은 매 방문마다 현재까지의 도구
        # 결과(자기 채널 기준 — 중기 #6)로 재계산됩니다 — 마지막 방문(도구
        # 호출 없음, 보고서 완성) 시점의 값이 도구 루프 전체의 결과를 반영한
        # 최종값이 되어 하류로 전달됩니다. verified_snapshot은 하류가 읽지
        # 않는 분석가 채널과 달리 별도 상태 필드라 하류 프롬프트 주입과
        # 사후 수치 감사에 쓰입니다(중기 #5).
        return {
            "market_messages": [result],
            "market_report": report,
            "market_data_ok": _market_data_ok(state["market_messages"]),
            "verified_snapshot": _extract_verified_snapshot(state["market_messages"]),
        }

    return market_analyst_node
