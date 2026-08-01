# =============================================================================
# [모듈 개요 — 초보자용]
# 이 파일은 TradingAgents(LLM 멀티 에이전트 주식 트레이딩 프레임워크)의
# 에이전트 상태(state) 구조를 정의합니다. LangGraph 그래프의 각 노드(에이전트)는
# 이 상태 딕셔너리를 읽고 자기 결과를 채워 다음 노드로 넘깁니다.
# TypedDict는 "이 딕셔너리에는 어떤 키가 어떤 타입으로 들어가는지"를 선언하는
# 타입 힌트용 클래스이며, Annotated[타입, "설명"]의 설명 문자열은 각 필드의
# 의미를 문서화합니다(프로그램 동작에 쓰이므로 영어 원문 유지).
# =============================================================================

from typing import Annotated

from langchain_core.messages import AnyMessage
from langgraph.graph import MessagesState
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

# 분석가 키 -> 전용 메시지 채널 이름 매핑 (설계분석 중기 로드맵 #6).
# 분석가 4종이 단일 messages 채널을 공유하면 LangGraph가 병렬 fan-out을
# 하더라도 서로의 도구 대화가 뒤섞이므로, 분석가마다 독립 채널을 둔다.
# Propagator(초기 상태 시드), ToolNode(messages_key), CLI(진행 표시)가
# 모두 이 매핑을 단일 소스로 참조한다.
ANALYST_MESSAGE_CHANNELS: dict[str, str] = {
    "market": "market_messages",
    "social": "social_messages",
    "news": "news_messages",
    "fundamentals": "fundamentals_messages",
}

# 그래프 상태에 존재하는 모든 메시지 채널(공유 + 분석가별). 디버그 출력과
# CLI 스트리밍 표시는 이 목록을 순회하며 새 메시지를 수집한다.
ALL_MESSAGE_CHANNELS: tuple[str, ...] = (
    "messages",
    *ANALYST_MESSAGE_CHANNELS.values(),
)


# 리서처(Researcher) 팀 상태 — 매수(강세)/매도(약세) 토론 진행 상황을 담는다.
class InvestDebateState(TypedDict):
    bull_history: Annotated[
        str, "Bullish Conversation history"
    ]  # 강세론자(bull) 대화 이력
    bear_history: Annotated[
        str, "Bearish Conversation history"
    ]  # 약세론자(bear) 대화 이력
    history: Annotated[str, "Conversation history"]  # 전체 대화 이력
    current_response: Annotated[str, "Latest response"]  # 마지막 응답
    judge_decision: Annotated[str, "Final judge decision"]  # 심판(judge)의 최종 결정
    count: Annotated[int, "Length of the current conversation"]  # 현재 대화 길이


# 리스크 관리(Risk Management) 팀 상태 — 공격/보수/중립 성향 토론 진행 상황을 담는다.
class RiskDebateState(TypedDict):
    aggressive_history: Annotated[
        str, "Aggressive Agent's Conversation history"
    ]  # 공격적(aggressive) 에이전트 대화 이력
    conservative_history: Annotated[
        str, "Conservative Agent's Conversation history"
    ]  # 보수적(conservative) 에이전트 대화 이력
    neutral_history: Annotated[
        str, "Neutral Agent's Conversation history"
    ]  # 중립(neutral) 에이전트 대화 이력
    history: Annotated[str, "Conversation history"]  # 전체 대화 이력
    latest_speaker: Annotated[str, "Analyst that spoke last"]
    current_aggressive_response: Annotated[
        str, "Latest response by the aggressive analyst"
    ]  # 공격적 분석가의 마지막 응답
    current_conservative_response: Annotated[
        str, "Latest response by the conservative analyst"
    ]  # 보수적 분석가의 마지막 응답
    current_neutral_response: Annotated[
        str, "Latest response by the neutral analyst"
    ]  # 중립 분석가의 마지막 응답
    judge_decision: Annotated[str, "Judge's decision"]
    count: Annotated[int, "Length of the current conversation"]  # 현재 대화 길이


# 전체 워크플로의 최상위 상태. LangGraph의 MessagesState를 상속하므로
# 대화 메시지 목록(messages)이 기본 포함되고, 그 위에 분석 대상 정보와
# 각 단계별 리포트/토론 상태 필드를 추가로 정의한다.
class AgentState(MessagesState):
    # ------------------------------------------------------------------
    # 분석가별 독립 메시지 채널 (설계분석 중기 로드맵 #6 — 분석가 병렬화).
    # 기존에는 분석가 4종이 상속받은 단일 messages 채널을 공유해서
    # (1) 직렬 실행이 강제되고, (2) 다음 분석가를 위해 Msg Clear로 대화를
    # 비우는 우회책이 필요했으며, (3) 그 과정에서 원본 도구 데이터가
    # 파기됐다(설계분석-보고서 2.2절). 분석가마다 자기 채널만 읽고 쓰게
    # 분리하면 START에서 4노드 fan-out이 가능해지고, Msg Clear가 불필요해져
    # 원본 도구 메시지가 실행 종료까지 보존된다.
    # 공유 messages 채널은 하위 호환(초기 시드 메시지, 트레이더의 결과
    # 기록, 디버그 출력)을 위해 유지하되 분석가 단계에서는 쓰지 않는다.
    # ------------------------------------------------------------------
    market_messages: Annotated[list[AnyMessage], add_messages]
    social_messages: Annotated[list[AnyMessage], add_messages]
    news_messages: Annotated[list[AnyMessage], add_messages]
    fundamentals_messages: Annotated[list[AnyMessage], add_messages]

    company_of_interest: Annotated[str, "Company that we are interested in trading"]
    asset_type: Annotated[str, "Asset type under analysis such as stock or crypto"]
    instrument_context: Annotated[str, "Deterministic ticker identity resolved at run start"]
    trade_date: Annotated[str, "What date we are trading at"]

    sender: Annotated[str, "Agent that sent this message"]

    # 리서치(분석) 단계 — 각 분석가가 작성한 리포트
    market_report: Annotated[str, "Report from the Market Analyst"]
    sentiment_report: Annotated[str, "Report from the Sentiment Analyst"]
    news_report: Annotated[
        str, "Report from the News Researcher of current world affairs"
    ]
    fundamentals_report: Annotated[str, "Report from the Fundamentals Researcher"]

    # NO_DATA 결정론적 게이트(설계분석 중기 로드맵 #4)용 기계 판독 플래그.
    # 시장 분석가 노드가 자신의 도구 결과(ToolMessage)에서 NO_DATA 센티널을
    # 발견하면 False로 기록하고, 포트폴리오 매니저는 LLM 호출 전에 이 값을
    # 검사해 핵심 시장 데이터 부재 시 결정론적으로 Hold를 강제합니다.
    # LLM 판단이 아니라 센티널 문자열 검사로만 설정됩니다.
    market_data_ok: Annotated[
        bool,
        "Deterministic flag: False when the market analyst's tool results "
        "contained a NO_DATA sentinel (core market data unavailable)",
    ]

    # 검증 스냅샷 보존(설계분석 중기 로드맵 #5). 하류 에이전트는 분석가의
    # 메시지 채널을 읽지 않으므로, 시장 분석가 노드가
    # get_verified_market_snapshot의 원본 출력을 이 별도 상태 필드로 옮겨
    # 보존합니다. (중기 #6 이전에는 Msg Clear가 도구 메시지를 파기해서 이
    # 필드가 유일한 생존 경로였고, 병렬화 이후에도 하류가 채널 구조를 몰라도
    # 되는 단일 기준점으로 계속 사용합니다.) 하류(토론자·트레이더·리서치
    # 매니저·PM)는 이 스냅샷을 "정확한 수치의 기준점"으로 프롬프트에
    # 주입받고, 실행 종료 후 결정문의 수치 대조(numeric audit)에도
    # 사용됩니다. NO_DATA 센티널이거나 스냅샷 도구가 호출되지 않았으면 빈
    # 문자열입니다. 구형 체크포인트에는 이 키가 없을 수 있으므로 소비자는
    # 반드시 .get()으로 접근해야 합니다.
    verified_snapshot: Annotated[
        str,
        "Verified market snapshot preserved from the market analyst's tool "
        "results into a dedicated state field; empty when unavailable",
    ]

    # 리서처 팀 토론 단계 — 투자 여부 토론 상태와 결과 계획
    investment_debate_state: Annotated[
        InvestDebateState, "Current state of the debate on if to invest or not"
    ]
    investment_plan: Annotated[str, "Plan generated by the Analyst"]

    trader_investment_plan: Annotated[str, "Plan generated by the Trader"]

    # 리스크 관리 팀 토론 단계 — 리스크 평가 토론 상태와 최종 결정
    risk_debate_state: Annotated[
        RiskDebateState, "Current state of the debate on evaluating risk"
    ]
    final_trade_decision: Annotated[str, "Final decision made by the Risk Analysts"]
    past_context: Annotated[str, "Memory log context injected at run start (same-ticker decisions + cross-ticker lessons)"]
