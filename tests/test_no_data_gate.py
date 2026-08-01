"""[모듈 개요] NO_DATA 결정론적 게이트(설계분석 중기 로드맵 #4) 테스트.

데이터가 없거나 벤더가 전부 실패하면 도구는 NO_DATA 센티널 텍스트를 반환하는데,
기존에는 "데이터 없이 값을 지어내지 마라"는 프롬프트 순응(확률적 방어)에만
의존했다. 이 테스트는 결정론적 게이트의 각 단계를 검증한다:

  1. NO_DATA 센티널 감지 로직(is_no_data_sentinel)이 결정론적으로 동작하는지
  2. 검증 스냅샷 도구가 데이터 부재 시 크래시 대신 센티널을 반환하는지
  3. 시장 분석가가 도구 결과의 센티널을 감지해 market_data_ok=False를 남기는지
  4. 포트폴리오 매니저가 플래그 False일 때 LLM 호출 없이 Hold를 강제하는지
  5. 강제 Hold가 메모리 로그에 저장되지 않는지 (학습 오염 방지)
  6. 존재하지 않는 티커 시나리오에서 최종 시그널이 Hold로 끝나는지 (통합)
"""

from unittest import mock
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from tradingagents.agents.analysts.market_analyst import (
    _market_data_ok,
    create_market_analyst,
)
from tradingagents.agents.managers.portfolio_manager import (
    FORCED_HOLD_REASON,
    create_portfolio_manager,
)
from tradingagents.agents.utils.market_data_validation_tools import (
    get_verified_market_snapshot,
)
from tradingagents.dataflows import interface
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.interface import is_no_data_sentinel
from tradingagents.graph.propagation import Propagator
from tradingagents.graph.signal_processing import SignalProcessor


def _router_sentinel(symbol: str = "FAKE") -> str:
    """모든 벤더가 데이터 없음을 보고할 때 라우터가 실제로 만드는 센티널을 얻는다."""

    def raises_no_data(sym, *a, **k):
        raise NoMarketDataError(sym, sym, "no rows")

    patched = {"yfinance": raises_no_data, "alpha_vantage": raises_no_data}
    with mock.patch.dict(
        interface.VENDOR_METHODS, {"get_stock_data": patched}, clear=False
    ):
        return interface.route_to_vendor(
            "get_stock_data", symbol, "2026-01-01", "2026-01-10"
        )


# ---------------------------------------------------------------------------
# 1. 센티널 감지 로직
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSentinelDetection:
    def test_detects_real_router_sentinel(self):
        """벤더 라우터가 실제로 생성한 NO_DATA 센티널을 감지하는지 검증하는 테스트."""
        assert is_no_data_sentinel(_router_sentinel()) is True

    def test_normal_data_is_not_flagged(self):
        """정상 데이터(CSV·마크다운 보고서)는 센티널로 오인하지 않는지 검증하는 테스트."""
        assert is_no_data_sentinel("Date,Open,High,Low,Close\n2026-01-02,1,2,3,4") is False
        assert is_no_data_sentinel("## Verified market data snapshot for AAPL") is False

    def test_optional_data_unavailable_is_not_flagged(self):
        """선택적 부가 데이터의 DATA_UNAVAILABLE 완화 센티널은 매칭하지 않는지 검증하는 테스트."""
        text = (
            "DATA_UNAVAILABLE: optional macro_data could not be retrieved. "
            "Proceed without it; do not fabricate values."
        )
        assert is_no_data_sentinel(text) is False

    def test_non_string_inputs_are_safe(self):
        """문자열이 아닌 입력(None, 숫자)에도 크래시 없이 False를 반환하는지 검증하는 테스트."""
        assert is_no_data_sentinel(None) is False
        assert is_no_data_sentinel(123) is False


# ---------------------------------------------------------------------------
# 2. 검증 스냅샷 도구의 우아한 실패 (크래시 경로 정리)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSnapshotGracefulFailure:
    def test_value_error_becomes_sentinel(self):
        """스냅샷 빌더의 ValueError가 크래시 대신 NO_DATA 센티널로 변환되는지 검증하는 테스트."""
        with mock.patch(
            "tradingagents.agents.utils.market_data_validation_tools."
            "build_verified_market_snapshot",
            side_effect=ValueError("No OHLCV data available for FAKE."),
        ):
            out = get_verified_market_snapshot.func("FAKE", "2026-01-01")
        assert is_no_data_sentinel(out)
        assert "FAKE" in out

    def test_no_market_data_error_becomes_sentinel(self):
        """load_ohlcv의 NoMarketDataError도 센티널로 변환되는지 검증하는 테스트."""
        with mock.patch(
            "tradingagents.agents.utils.market_data_validation_tools."
            "build_verified_market_snapshot",
            side_effect=NoMarketDataError("FAKE", "FAKE", "empty download"),
        ):
            out = get_verified_market_snapshot.func("FAKE", "2026-01-01")
        assert is_no_data_sentinel(out)

    def test_success_path_untouched(self):
        """데이터가 있으면 스냅샷 본문이 그대로 반환되는지 검증하는 테스트."""
        with mock.patch(
            "tradingagents.agents.utils.market_data_validation_tools."
            "build_verified_market_snapshot",
            return_value="## Verified market data snapshot for AAPL",
        ):
            out = get_verified_market_snapshot.func("AAPL", "2026-01-01")
        assert out.startswith("## Verified market data snapshot")
        assert not is_no_data_sentinel(out)


# ---------------------------------------------------------------------------
# 3. 시장 분석가의 기계 판독 플래그
# ---------------------------------------------------------------------------


class _ReportOnlyLLM:
    """bind_tools 후 도구 호출 없는 최종 보고서만 반환하는 가짜 LLM.

    ChatPromptTemplate | callable 파이프가 callable을 RunnableLambda로
    감싸므로, 호출 가능한 객체를 돌려주는 것만으로 체인이 동작한다.
    """

    def bind_tools(self, tools):
        return lambda _prompt_value: AIMessage(content="final market report")


def _analyst_state(tool_content: str, asset_type: str = "stock") -> dict:
    # 시장 분석가는 병렬화(중기 로드맵 #6) 이후 공유 messages 채널이 아니라
    # 전용 채널(market_messages)만 읽으므로, 테스트 상태도 그 채널에 도구
    # 결과를 담는다.
    return {
        "trade_date": "2026-01-01",
        "company_of_interest": "FAKE",
        "asset_type": asset_type,
        "instrument_context": "Instrument: FAKE",
        "market_messages": [
            HumanMessage(content="FAKE"),
            AIMessage(
                content="",
                tool_calls=[{"name": "get_stock_data", "args": {}, "id": "call1"}],
            ),
            ToolMessage(content=tool_content, tool_call_id="call1"),
        ],
    }


@pytest.mark.unit
class TestMarketAnalystFlag:
    def test_sentinel_in_tool_result_sets_flag_false(self):
        """도구 결과에 NO_DATA 센티널이 있으면 market_data_ok=False를 남기는지 검증하는 테스트."""
        node = create_market_analyst(_ReportOnlyLLM())
        result = node(_analyst_state(_router_sentinel()))
        assert result["market_data_ok"] is False
        assert result["market_report"] == "final market report"

    def test_normal_tool_result_keeps_flag_true(self):
        """정상 도구 결과에서는 market_data_ok=True를 유지하는지 검증하는 테스트."""
        node = create_market_analyst(_ReportOnlyLLM())
        result = node(_analyst_state("Date,Open,Close\n2026-01-02,1,2"))
        assert result["market_data_ok"] is True

    def test_crypto_asset_mode_also_flags(self):
        """크립토 자산 모드(asset_type=crypto)에서도 게이트가 동일하게 동작하는지 검증하는 테스트."""
        node = create_market_analyst(_ReportOnlyLLM())
        result = node(_analyst_state(_router_sentinel("FAKE-USD"), asset_type="crypto"))
        assert result["market_data_ok"] is False

    def test_detector_ignores_non_tool_messages(self):
        """AI/Human 메시지 본문에 센티널 문구가 있어도 도구 결과가 아니면 무시하는지 검증하는 테스트."""
        messages = [
            HumanMessage(content="NO_DATA_AVAILABLE mentioned by a human"),
            AIMessage(content="the model discussed NO_DATA_AVAILABLE in prose"),
        ]
        assert _market_data_ok(messages) is True

    def test_initial_state_defaults_flag_true(self):
        """Propagator 초기 상태의 기본값이 True(낙관적)인지 검증하는 테스트."""
        state = Propagator().create_initial_state("AAPL", "2026-01-01")
        assert state["market_data_ok"] is True


# ---------------------------------------------------------------------------
# 4. 포트폴리오 매니저의 강제 분기
# ---------------------------------------------------------------------------


def _pm_state(**extra) -> dict:
    state = {
        "company_of_interest": "FAKE",
        "risk_debate_state": {
            "history": "h", "aggressive_history": "a", "conservative_history": "c",
            "neutral_history": "n", "current_aggressive_response": "",
            "current_conservative_response": "", "current_neutral_response": "",
            "latest_speaker": "Neutral", "judge_decision": "", "count": 1,
        },
        "investment_plan": "plan",
        "trader_investment_plan": "trader plan",
    }
    state.update(extra)
    return state


@pytest.mark.unit
class TestPortfolioManagerGate:
    def test_forced_hold_without_llm_call(self):
        """플래그 False일 때 LLM 호출 없이 결정론적 Hold를 반환하는지 검증하는 테스트."""
        llm = MagicMock()
        node = create_portfolio_manager(llm)
        result = node(_pm_state(market_data_ok=False))

        # 구조화 경로도, 자유 텍스트 폴백 경로도 호출되지 않아야 한다.
        llm.with_structured_output.return_value.invoke.assert_not_called()
        llm.invoke.assert_not_called()

        decision = result["final_trade_decision"]
        assert FORCED_HOLD_REASON in decision
        assert SignalProcessor(None).process_signal(decision) == "Hold"
        # 판정 결과가 리스크 토론 상태에도 정상 경로와 동일하게 기록되어야 한다.
        assert result["risk_debate_state"]["judge_decision"] == decision
        assert result["risk_debate_state"]["latest_speaker"] == "Judge"
        # 토론 이력은 그대로 보존되어야 한다.
        assert result["risk_debate_state"]["history"] == "h"

    def test_normal_path_when_flag_true(self):
        """플래그 True일 때 기존 LLM 판정 경로가 그대로 동작하는지 검증하는 테스트."""
        from tradingagents.agents.schemas import PortfolioDecision, PortfolioRating

        structured = MagicMock()
        structured.invoke.return_value = PortfolioDecision(
            rating=PortfolioRating.BUY,
            executive_summary="x",
            investment_thesis="y",
        )
        llm = MagicMock()
        llm.with_structured_output.return_value = structured

        result = create_portfolio_manager(llm)(_pm_state(market_data_ok=True))
        structured.invoke.assert_called_once()
        assert SignalProcessor(None).process_signal(result["final_trade_decision"]) == "Buy"

    def test_missing_flag_defaults_to_normal_path(self):
        """플래그 키가 없는 상태(구형 체크포인트·최소 상태)에서 기존 동작을 유지하는지 검증하는 테스트."""
        from tradingagents.agents.schemas import PortfolioDecision, PortfolioRating

        structured = MagicMock()
        structured.invoke.return_value = PortfolioDecision(
            rating=PortfolioRating.HOLD,
            executive_summary="x",
            investment_thesis="y",
        )
        llm = MagicMock()
        llm.with_structured_output.return_value = structured

        create_portfolio_manager(llm)(_pm_state())
        structured.invoke.assert_called_once()


# ---------------------------------------------------------------------------
# 5. 메모리 오염 방지 (강제 Hold는 저장하지 않음)
# ---------------------------------------------------------------------------


def _graph_harness(tmp_path, final_state):
    """LLM 생성 없이 _run_graph의 저장 분기만 실행하는 최소 하네스를 만든다."""
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    g = object.__new__(TradingAgentsGraph)
    g.config = {"results_dir": str(tmp_path)}
    g.debug = False
    g.ticker = final_state["company_of_interest"]
    g.memory_log = MagicMock()
    g.memory_log.get_past_context.return_value = ""
    g.propagator = Propagator()
    g.graph = MagicMock()
    g.graph.invoke.return_value = final_state
    g.signal_processor = SignalProcessor(None)
    g.log_states_dict = {}
    # 인스턴스 속성으로 메서드를 가려 네트워크 조회(yfinance)를 차단한다.
    g.resolve_instrument_context = lambda ticker, asset_type="stock": ""
    return g


def _final_state(decision: str, market_data_ok: bool) -> dict:
    state = Propagator().create_initial_state("FAKE", "2026-01-01")
    state.update(
        {
            "investment_plan": "plan",
            "trader_investment_plan": "trader plan",
            "final_trade_decision": decision,
            "market_data_ok": market_data_ok,
        }
    )
    state["risk_debate_state"]["judge_decision"] = decision
    return state


@pytest.mark.unit
class TestMemoryPollutionGuard:
    def test_forced_hold_is_not_stored(self, tmp_path):
        """강제 Hold(market_data_ok=False)가 메모리 로그에 저장되지 않는지 검증하는 테스트."""
        from tradingagents.agents.managers.portfolio_manager import FORCED_HOLD_DECISION

        g = _graph_harness(tmp_path, _final_state(FORCED_HOLD_DECISION, False))
        _, signal = g._run_graph("FAKE", "2026-01-01")
        g.memory_log.store_decision.assert_not_called()
        assert signal == "Hold"

    def test_normal_decision_is_stored(self, tmp_path):
        """정상 결정(market_data_ok=True)은 기존대로 메모리 로그에 저장되는지 검증하는 테스트."""
        g = _graph_harness(tmp_path, _final_state("**Rating**: Buy", True))
        _, signal = g._run_graph("FAKE", "2026-01-01")
        g.memory_log.store_decision.assert_called_once()
        assert signal == "Buy"


# ---------------------------------------------------------------------------
# 6. 통합: 존재하지 않는 티커 → 최종 시그널 Hold
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_missing_ticker_scenario_ends_in_hold():
    """존재하지 않는 티커(전 벤더 데이터 없음)가 강제 Hold 시그널로 끝나는지 검증하는 통합 테스트.

    실제 벤더 라우터가 만든 NO_DATA 센티널 → 시장 분석가의 플래그 감지 →
    포트폴리오 매니저의 결정론적 분기 → 시그널 추출까지 전 구간을 잇는다.
    """
    sentinel = _router_sentinel("ZZZZFAKE")

    # 1단계: 시장 분석가가 센티널을 감지해 플래그를 내린다.
    analyst_result = create_market_analyst(_ReportOnlyLLM())(
        _analyst_state(sentinel)
    )
    assert analyst_result["market_data_ok"] is False

    # 2단계: 포트폴리오 매니저가 LLM 호출 없이 Hold를 강제한다.
    pm_llm = MagicMock()
    pm_result = create_portfolio_manager(pm_llm)(
        _pm_state(
            market_data_ok=analyst_result["market_data_ok"],
            market_report=analyst_result["market_report"],
        )
    )
    pm_llm.with_structured_output.return_value.invoke.assert_not_called()
    pm_llm.invoke.assert_not_called()

    # 3단계: 시그널 파서가 결정론적으로 Hold를 추출한다.
    assert SignalProcessor(None).process_signal(pm_result["final_trade_decision"]) == "Hold"
