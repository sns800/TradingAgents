# =============================================================================
# [모듈 개요 — 초보자용]
# 이 패키지는 TradingAgents의 "평가·백테스트 하네스"입니다. 설계 분석 보고서의
# 중기 로드맵 #1에 해당하며, "멀티 에이전트 토론이 단일 LLM보다 낫다",
# "반성 루프가 학습 효과를 낸다" 같은 설계 가정을 측정 가능하게 만드는 것이
# 목적입니다. 세 부분으로 구성됩니다:
#   - scoreboard: 메모리 로그의 해소(resolved)된 결정을 집계해 등급별
#     방향 적중률·평균 알파를 계산하고 베이스라인과 비교
#   - backtest:   과거 날짜 범위를 일괄 실행하고 복수 보유기간(1/5/20 거래일)
#     수익률을 사후 계산하는 백테스트 러너
#   - baseline:   에이전트 파이프라인 없이 단일 LLM 1회 호출로 등급을 받는
#     비교용 베이스라인
# =============================================================================

"""TradingAgents 평가·백테스트 하네스 패키지."""

from tradingagents.eval.scoreboard import (
    aggregate_entries,
    is_directional_hit,
    parse_percent,
    render_markdown,
)

__all__ = [
    "aggregate_entries",
    "is_directional_hit",
    "parse_percent",
    "render_markdown",
]
