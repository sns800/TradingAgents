"""포트폴리오 매니저의 결정문에서 5단계 등급(rating)을 추출하는 모듈.

[모듈 개요 - 초보자용]
그래프 실행이 끝나면 포트폴리오 매니저(Portfolio Manager)가 긴 결정문을
내놓는데, 그중에서 최종 등급(Buy/Overweight/Hold/Underweight/Sell) 한 단어만
뽑아내는 것이 이 모듈의 역할입니다. trading_graph.py의 process_signal()이
사용합니다.

포트폴리오 매니저는 구조화 출력(structured output)으로 타입이 정해진
``PortfolioDecision``을 생성하고, 이를 항상 ``**Rating**: X`` 헤더가 포함된
마크다운으로 렌더링합니다(:func:`tradingagents.agents.schemas.render_pm_decision`
참고). 그 등급을 추출하는 데에는
:mod:`tradingagents.agents.utils.rating`의 결정적(deterministic) 휴리스틱만으로
충분하며, 추가 LLM 호출은 필요하지 않습니다.

이 모듈은 ``SignalProcessor.process_signal(text)`` 인터페이스를 기대하는
기존 호출자와의 하위 호환(backwards compatibility)을 위해 존재합니다.
"""

from __future__ import annotations

from typing import Any

from tradingagents.agents.utils.rating import parse_rating


class SignalProcessor:
    """포트폴리오 매니저의 결정문에서 5단계 등급을 읽어내는 클래스."""

    def __init__(self, quick_thinking_llm: Any = None):
        # LLM 인자는 하위 호환을 위해 받기만 하고 더 이상 사용하지 않습니다.
        # 포트폴리오 매니저의 구조화 출력 덕분에, 렌더링된 마크다운에서
        # 두 번째 LLM 호출 없이도 등급을 파싱할 수 있음이 보장됩니다.
        self.quick_thinking_llm = quick_thinking_llm

    def process_signal(self, full_signal: str) -> str:
        """Buy / Overweight / Hold / Underweight / Sell 중 하나를 반환한다."""
        return parse_rating(full_signal, context="portfolio decision signal")
