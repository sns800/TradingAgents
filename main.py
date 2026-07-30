# =============================================================================
# [모듈 개요 - 초보자용]
# 이 파일은 TradingAgents(LLM 멀티 에이전트 주식 트레이딩 프레임워크)를
# 파이썬 코드에서 직접 실행하는 가장 간단한 예제 스크립트입니다.
# CLI(명령줄 인터페이스) 없이, 트레이딩 그래프(TradingAgentsGraph)를 만들어
# 특정 종목(예: NVDA)과 날짜에 대한 매매 결정을 한 번 실행해 봅니다.
# 프레임워크 사용법을 익히는 출발점(entry point)으로 보면 됩니다.
# =============================================================================

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

# DEFAULT_CONFIG는 이미 TRADINGAGENTS_* 환경변수(environment variable) 오버라이드를
# 반영한 상태입니다(llm_provider, deep_think_llm, quick_think_llm, backend_url 등).
# 따라서 이 스크립트를 수정하지 않고도 .env 파일만으로 모델이나 엔드포인트(endpoint)를
# 바꿀 수 있습니다. 환경변수를 무시하고 강제로 고정하고 싶은 값이 있을 때만
# 아래에서 개별 키를 덮어쓰세요.
config = DEFAULT_CONFIG.copy()

# 커스텀 설정으로 초기화
ta = TradingAgentsGraph(debug=True, config=config)

# 순전파(forward propagate): 에이전트들이 분석~결정까지 한 번 실행됨
_, decision = ta.propagate("NVDA", "2024-05-10")
print(decision)

# 실수를 기억하고 반성(reflect)하기
# ta.reflect_and_remember(1000) # 매개변수는 포지션 수익률(position returns)
