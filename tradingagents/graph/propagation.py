# TradingAgents/graph/propagation.py
#
# [모듈 개요 - 초보자용]
# 이 파일은 그래프(graph) 실행에 필요한 초기 상태(state, 에이전트들이 읽고 쓰는
# 공유 데이터 딕셔너리)를 만들고, 그래프 호출 인자(recursion_limit, 콜백 등)를
# 구성하는 역할을 합니다. trading_graph.py가 분석을 시작할 때 여기서 만든
# 초기 상태를 그래프에 흘려보내며(propagate), 토론 상태·보고서 칸 등이
# 모두 빈 값으로 준비됩니다. 아래 딕셔너리 키들은 다른 코드가 그대로
# 참조하므로 절대 바꾸면 안 됩니다.

from typing import Any

from tradingagents.agents.utils.agent_states import (
    InvestDebateState,
    RiskDebateState,
)


class Propagator:
    """그래프 전체에 걸친 상태 초기화와 전파(propagation)를 담당하는 클래스."""

    def __init__(self, max_recur_limit=100):
        """설정 파라미터로 초기화한다."""
        self.max_recur_limit = max_recur_limit

    def create_initial_state(
        self,
        company_name: str,
        trade_date: str,
        asset_type: str = "stock",
        past_context: str = "",
        instrument_context: str = "",
    ) -> dict[str, Any]:
        """에이전트 그래프의 초기 상태를 생성한다.

        ``instrument_context``는 실행 시작 시 한 번만 결정적으로(deterministic)
        조회한 티커 정체성(ticker identity) 문자열입니다
        (``TradingAgentsGraph.resolve_instrument_context`` 참고). 빈 문자열이면
        에이전트들은 ``get_instrument_context_from_state``를 통해 티커만 담긴
        컨텍스트로 대체(fallback)합니다.
        """
        return {
            "messages": [("human", company_name)],
            "company_of_interest": company_name,
            "asset_type": asset_type,
            "instrument_context": instrument_context,
            "trade_date": str(trade_date),
            "past_context": past_context,
            # 강세/약세 투자 토론의 진행 상황을 담는 상태. count는 발언 횟수로,
            # conditional_logic.py가 토론 종료 여부를 판단할 때 사용합니다.
            "investment_debate_state": InvestDebateState(
                {
                    "bull_history": "",
                    "bear_history": "",
                    "history": "",
                    "current_response": "",
                    "judge_decision": "",
                    "count": 0,
                }
            ),
            # 리스크 토론(공격적/보수적/중립 3자 토론)의 진행 상황을 담는 상태.
            "risk_debate_state": RiskDebateState(
                {
                    "aggressive_history": "",
                    "conservative_history": "",
                    "neutral_history": "",
                    "history": "",
                    "latest_speaker": "",
                    "current_aggressive_response": "",
                    "current_conservative_response": "",
                    "current_neutral_response": "",
                    "judge_decision": "",
                    "count": 0,
                }
            ),
            "market_report": "",
            "fundamentals_report": "",
            "sentiment_report": "",
            "news_report": "",
        }

    def get_graph_args(self, callbacks: list | None = None) -> dict[str, Any]:
        """그래프 호출(invocation)에 넘길 인자들을 구성한다.

        Args:
            callbacks: 도구 실행 추적용 콜백 핸들러 목록(선택).
                       참고: LLM 콜백은 LLM 생성자를 통해 별도로 처리됩니다.
        """
        config = {"recursion_limit": self.max_recur_limit}
        if callbacks:
            config["callbacks"] = callbacks
        return {
            "stream_mode": "values",
            "config": config,
        }
