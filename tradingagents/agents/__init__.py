# =============================================================================
# [모듈 개요 - 초보자용]
# tradingagents.agents 서브패키지의 진입점으로, 각 에이전트 생성 함수와 상태
# 클래스를 한곳에 모아 내보내는(re-export) 역할만 합니다.
# 전체 파이프라인의 등장인물이 모두 여기 나열되어 있습니다:
#   분석가(Analyst: 시장/뉴스/심리/펀더멘털) → 리서처 토론(강세론자 Bull vs
#   약세론자 Bear, 리서치 매니저가 중재) → 트레이더(Trader) → 리스크 토론
#   (공격적/보수적/중립적 애널리스트) → 포트폴리오 매니저(Portfolio Manager).
# =============================================================================

from .analysts.fundamentals_analyst import create_fundamentals_analyst
from .analysts.market_analyst import create_market_analyst
from .analysts.news_analyst import create_news_analyst
from .analysts.sentiment_analyst import (
    create_sentiment_analyst,
    create_social_media_analyst,  # 지원 중단(deprecated)된 별칭. 하위 호환성 유지용
)
from .managers.portfolio_manager import create_portfolio_manager
from .managers.research_manager import create_research_manager
from .researchers.bear_researcher import create_bear_researcher
from .researchers.bull_researcher import create_bull_researcher
from .risk_mgmt.aggressive_debator import create_aggressive_debator
from .risk_mgmt.conservative_debator import create_conservative_debator
from .risk_mgmt.neutral_debator import create_neutral_debator
from .trader.trader import create_trader

# create_msg_delete는 분석가 병렬화(중기 로드맵 #6)로 Msg Clear 노드가
# 제거되면서 함께 삭제됐습니다 (agent_utils.py 참고).
from .utils.agent_states import AgentState, InvestDebateState, RiskDebateState

__all__ = [
    "AgentState",
    "InvestDebateState",
    "RiskDebateState",
    "create_bear_researcher",
    "create_bull_researcher",
    "create_research_manager",
    "create_fundamentals_analyst",
    "create_market_analyst",
    "create_neutral_debator",
    "create_news_analyst",
    "create_aggressive_debator",
    "create_portfolio_manager",
    "create_conservative_debator",
    "create_sentiment_analyst",
    "create_social_media_analyst",  # 지원 중단(deprecated). 향후 버전에서 제거 예정
    "create_trader",
]
