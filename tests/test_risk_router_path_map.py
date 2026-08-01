"""[모듈 개요] 공용 라우터(router)와 경로 맵(path_map)의 완전성을 검증하는 테스트 (#1088).

`should_continue_risk_analysis`(리스크 간선(edge) 3개)와
`should_continue_debate`(리서치 토론 간선 2개)는 모두 단일 라우터인데,
그 반환값 집합이 예전에 개별 간선에 매핑된 것보다 크다. 이제 각 간선은
완전한 경로 맵(`RISK_ANALYSIS_PATH_MAP` / `DEBATE_PATH_MAP`)을 공유하므로,
폴스루(fall-through) 반환이 누락된 항목에 걸리는 일이 없다 — 누락되면
화자(speaker) 라벨의 프롬프트/국제화(i18n)/리팩터링 변화 때 LangGraph가
실행 도중 크래시했을 것이다.
"""
import pytest

from tradingagents.graph.conditional_logic import ConditionalLogic
from tradingagents.graph.setup import DEBATE_PATH_MAP, RISK_ANALYSIS_PATH_MAP


def _state(latest_speaker, count=0):
    return {"risk_debate_state": {"latest_speaker": latest_speaker, "count": count}}


def _debate_state(current_response, count=0):
    return {"investment_debate_state": {"current_response": current_response, "count": count}}


@pytest.mark.unit
@pytest.mark.parametrize("latest_speaker", [
    "Aggressive", "Aggressive Analyst",
    "Conservative", "Conservative Analyst",
    "Neutral", "Neutral Analyst",
    "",                          # 변형(drift): 빈 라벨
    "Aggressive Risk Analyst",   # 변형(drift): 노드 이름 변경
    "Agresivo",                  # 변형(drift): 국제화(i18n)/번역된 라벨
])
def test_router_return_always_routable(latest_speaker):
    """리스크 라우터의 반환값이 어떤 화자 라벨이든 항상 경로 맵에 존재하는지 검증하는 테스트."""
    logic = ConditionalLogic(max_risk_discuss_rounds=1)
    target = logic.should_continue_risk_analysis(_state(latest_speaker))
    assert target in RISK_ANALYSIS_PATH_MAP


@pytest.mark.unit
def test_router_terminates_at_round_limit():
    """토론 라운드 한도에 도달하면 라우터가 토론을 종료시키는지 검증하는 테스트.

    응답 보장 종료 조건(중기 로드맵 #3)으로 종료 시점이 3N에서 3N+1로
    바뀌었다: count=3(예전 종료점)에서는 선발언자 Aggressive가 마지막 비판에
    재반박하도록 토론을 계속하고, count=4(3N+1)에서 종료한다.
    """
    logic = ConditionalLogic(max_risk_discuss_rounds=1)
    # count == 3N에서는 Neutral 직후 -> Aggressive의 마지막 재반박 차례
    assert logic.should_continue_risk_analysis(_state("Neutral", count=3)) == "Aggressive Analyst"
    # count >= 3N+1이면 포트폴리오 매니저(Portfolio Manager)로 라우팅 (토론 종료)
    assert logic.should_continue_risk_analysis(_state("Aggressive", count=4)) == "Portfolio Manager"


@pytest.mark.unit
def test_path_map_covers_full_router_range():
    """리스크 라우터가 낼 수 있는 모든 반환값을 경로 맵이 포괄하는지 검증하는 테스트."""
    logic = ConditionalLogic(max_risk_discuss_rounds=1)
    returns = {
        logic.should_continue_risk_analysis(_state(s, c))
        for s in ("Aggressive", "Conservative", "Neutral", "drift")
        for c in (0, 99)
    }
    # 라우터가 낼 수 있는 모든 값이 공용 맵의 키이고...
    assert returns <= set(RISK_ANALYSIS_PATH_MAP)
    # ...종착(terminal) 대상에도 도달할 수 있어야 한다.
    assert "Portfolio Manager" in returns


@pytest.mark.unit
@pytest.mark.parametrize("current_response", [
    "Bull", "Bull Researcher", "Bear", "Bear Researcher",
    "",                       # 변형(drift): 빈 라벨
    "Optimista",              # 변형(drift): 국제화(i18n)/번역된 라벨
])
def test_debate_router_return_always_routable(current_response):
    """토론 라우터의 반환값이 어떤 응답 라벨이든 항상 경로 맵에 존재하는지 검증하는 테스트."""
    logic = ConditionalLogic(max_debate_rounds=1)
    target = logic.should_continue_debate(_debate_state(current_response))
    assert target in DEBATE_PATH_MAP


@pytest.mark.unit
def test_debate_path_map_covers_full_router_range():
    """토론 라우터가 낼 수 있는 모든 반환값을 경로 맵이 포괄하는지 검증하는 테스트."""
    logic = ConditionalLogic(max_debate_rounds=1)
    returns = {
        logic.should_continue_debate(_debate_state(s, c))
        for s in ("Bull", "Bear", "drift")
        for c in (0, 99)
    }
    assert returns <= set(DEBATE_PATH_MAP)
    assert "Research Manager" in returns  # 종착 대상 도달 가능
