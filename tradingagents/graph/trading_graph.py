# TradingAgents/graph/trading_graph.py
#
# [모듈 개요 - 초보자용]
# 이 파일은 TradingAgents의 "심장"에 해당하는 핵심 파일로, LangGraph로 전체
# 에이전트 워크플로를 조립·실행하는 TradingAgentsGraph 클래스를 정의합니다.
# LLM 클라이언트 생성, 도구 노드(tool node) 구성, 그래프(graph, 에이전트들의
# 실행 순서를 정의한 흐름도) 컴파일, 체크포인트(checkpoint) 재개, 실행 결과
# 저장, 리플렉션(reflection, 과거 결정 복기)과 메모리 로그 갱신까지 한 번의
# propagate() 호출로 이어지는 전 과정을 이 클래스가 지휘합니다.
# CLI와 프로그래밍 방식 호출자(파이썬 코드) 모두 이 클래스를 진입점으로 씁니다.

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yfinance as yf
from langgraph.prebuilt import ToolNode

# agent_utils에서 추상화된 도구(tool) 함수들을 임포트
from tradingagents.agents.utils.agent_utils import (
    build_instrument_context,
    get_balance_sheet,
    get_cashflow,
    get_fundamentals,
    get_global_news,
    get_income_statement,
    get_indicators,
    get_insider_transactions,
    get_macro_indicators,
    get_news,
    get_prediction_markets,
    get_stock_data,
    get_verified_market_snapshot,
    resolve_instrument_identity,
)
from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.utils import safe_ticker_component
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.llm_clients import create_llm_client
from tradingagents.reporting import write_report_tree

from .checkpointer import checkpoint_step, clear_checkpoint, get_checkpointer, thread_id
from .conditional_logic import ConditionalLogic
from .propagation import Propagator
from .reflection import Reflector
from .setup import GraphSetup
from .signal_processing import SignalProcessor

logger = logging.getLogger(__name__)


def _coerce_max_retries(value):
    """``llm_max_retries`` 값을 검증해 0 이상의 정수로 변환한다.

    정수 또는 숫자 문자열(환경 변수는 문자열로 들어옵니다)을 허용합니다.
    불리언과 음수는 큰 소리로 거부해서, 잘못된 설정이 재시도를 조용히
    꺼 버리는 대신 시작 시점에 실패하도록 합니다.
    """
    if isinstance(value, bool):
        raise ValueError(f"llm_max_retries must be an integer, not a boolean: {value!r}")
    try:
        n = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"llm_max_retries must be an integer, got {value!r}") from exc
    if n < 0:
        raise ValueError(f"llm_max_retries must be >= 0, got {n}")
    return n


class TradingAgentsGraph:
    """트레이딩 에이전트 프레임워크 전체를 지휘(orchestrate)하는 메인 클래스."""

    def __init__(
        self,
        selected_analysts=("market", "social", "news", "fundamentals"),
        debug=False,
        config: dict[str, Any] = None,
        callbacks: list | None = None,
    ):
        """트레이딩 에이전트 그래프와 구성 요소들을 초기화한다.

        Args:
            selected_analysts: 포함할 애널리스트 종류의 목록
            debug: 디버그 모드로 실행할지 여부
            config: 설정 딕셔너리. None이면 기본 설정을 사용
            callbacks: 콜백 핸들러 목록(선택, 예: LLM/도구 사용량 통계 추적)
        """
        self.debug = debug
        self.config = config or DEFAULT_CONFIG
        self.callbacks = callbacks or []

        # 데이터플로 인터페이스의 설정을 갱신
        set_config(self.config)

        # 필요한 디렉터리 생성
        os.makedirs(self.config["data_cache_dir"], exist_ok=True)
        os.makedirs(self.config["results_dir"], exist_ok=True)

        # 제공자(provider)별 사고(thinking) 설정을 반영해 LLM 초기화
        llm_kwargs = self._get_provider_kwargs()

        # 콜백이 있으면 kwargs에 추가 (LLM 생성자로 전달됨)
        if self.callbacks:
            llm_kwargs["callbacks"] = self.callbacks

        # 깊은 사고용(deep)과 빠른 사고용(quick) LLM 클라이언트를 각각 생성.
        # 중요한 종합 판단(매니저급)은 deep, 잦은 호출(애널리스트급)은 quick이 담당.
        deep_client = create_llm_client(
            provider=self.config["llm_provider"],
            model=self.config["deep_think_llm"],
            base_url=self.config.get("backend_url"),
            **llm_kwargs,
        )
        quick_client = create_llm_client(
            provider=self.config["llm_provider"],
            model=self.config["quick_think_llm"],
            base_url=self.config.get("backend_url"),
            **llm_kwargs,
        )

        self.deep_thinking_llm = deep_client.get_llm()
        self.quick_thinking_llm = quick_client.get_llm()

        self.memory_log = TradingMemoryLog(self.config)

        # 도구 노드(tool node) 생성
        self.tool_nodes = self._create_tool_nodes()

        # 구성 요소 초기화
        self.conditional_logic = ConditionalLogic(
            max_debate_rounds=self.config["max_debate_rounds"],
            max_risk_discuss_rounds=self.config["max_risk_discuss_rounds"],
        )
        self.graph_setup = GraphSetup(
            self.quick_thinking_llm,
            self.deep_thinking_llm,
            self.tool_nodes,
            self.conditional_logic,
        )

        self.propagator = Propagator(
            max_recur_limit=self.config.get("max_recur_limit", 100),
        )
        self.reflector = Reflector(self.quick_thinking_llm)
        self.signal_processor = SignalProcessor(self.quick_thinking_llm)

        # 상태 추적용 변수들
        self.curr_state = None
        self.ticker = None
        self.log_states_dict = {}  # 날짜 -> 전체 상태 딕셔너리

        # 그래프 모양에 영향을 주는 실행 옵션. 체크포인트 시그니처(signature)에 사용.
        self.selected_analysts = tuple(selected_analysts)

        # 그래프 구성: 체크포인터(checkpointer)와 함께 다시 컴파일할 수 있도록
        # 컴파일 전의 workflow 객체를 보관해 둔다.
        self.workflow = self.graph_setup.setup_graph(selected_analysts)
        self.graph = self.workflow.compile()
        self._checkpointer_ctx = None

    def _get_provider_kwargs(self) -> dict[str, Any]:
        """LLM 클라이언트 생성에 쓸 제공자(provider)별 kwargs를 구성한다."""
        kwargs = {}
        provider = self.config.get("llm_provider", "").lower()

        if provider == "google":
            thinking_level = self.config.get("google_thinking_level")
            if thinking_level:
                kwargs["thinking_level"] = thinking_level

        elif provider == "openai":
            reasoning_effort = self.config.get("openai_reasoning_effort")
            if reasoning_effort:
                kwargs["reasoning_effort"] = reasoning_effort

        elif provider == "anthropic":
            effort = self.config.get("anthropic_effort")
            if effort:
                kwargs["effort"] = effort

        # 샘플링 온도(temperature)는 모든 제공자 공통: 설정되어 있으면 전달한다.
        # TRADINGAGENTS_TEMPERATURE 환경 변수에서 문자열("0.2")로 들어온 값도
        # 프로그래밍 방식의 float와 똑같이 동작하도록 float()로 변환한다.
        temperature = self.config.get("temperature")
        if temperature is not None and temperature != "":
            kwargs["temperature"] = float(temperature)

        # SDK 재시도(retry) 한도도 모든 제공자 공통. 명시적으로 설정된 경우에만
        # 전달해서, 그 외에는 각 제공자의 기본값(보통 2)을 유지한다 (#1091).
        max_retries = self.config.get("llm_max_retries")
        if max_retries is not None and max_retries != "":
            kwargs["max_retries"] = _coerce_max_retries(max_retries)

        return kwargs

    def _create_tool_nodes(self) -> dict[str, ToolNode]:
        """추상화된 도구 함수들로 데이터 소스별 도구 노드(tool node)를 만든다."""
        return {
            "market": ToolNode(
                [
                    # 핵심 주가 데이터 도구
                    get_stock_data,
                    # 기술적 지표
                    get_indicators,
                    # 결정적(deterministic) 검증 스냅샷 (애널리스트 LLM에
                    # 바인딩되어 있고 프롬프트가 호출을 요구하는 도구이므로,
                    # 여기서 실행 가능해야 합니다. 없으면 호출이 실패하고
                    # 모델이 "사용 불가"라고 보고하게 됩니다).
                    get_verified_market_snapshot,
                ]
            ),
            "social": ToolNode(
                [
                    # 소셜 미디어 감성 분석용 뉴스 도구
                    get_news,
                ]
            ),
            "news": ToolNode(
                [
                    # 뉴스와 내부자 거래 정보
                    get_news,
                    get_global_news,
                    get_insider_transactions,
                    get_macro_indicators,
                    get_prediction_markets,
                ]
            ),
            "fundamentals": ToolNode(
                [
                    # 재무 분석 도구
                    get_fundamentals,
                    get_balance_sheet,
                    get_cashflow,
                    get_income_statement,
                ]
            ),
        }

    def _resolve_benchmark(self, ticker: str) -> str:
        """``ticker``의 알파(alpha) 계산에 쓸 벤치마크 티커를 고른다.

        ``config["benchmark_ticker"]``가 설정되어 있으면 그것이 모든 것에
        우선합니다. 아니면 접미사 매핑(suffix map)이 티커의 거래소 접미사
        (예: 도쿄는 ``.T``)와 대조합니다. 점 접미사가 없는 미국 상장 티커는
        빈 접미사 항목(기본값 SPY)으로 넘어갑니다. 인식되지 않는 접미사
        (``BRK.B``처럼 점이 들어간 미국 티커 포함)도 빈 접미사 항목으로
        대체되는데, 알파 계산이 USD 기준으로 동작하므로 이것이 올바른
        기본값입니다.
        """
        explicit = self.config.get("benchmark_ticker")
        if explicit:
            return explicit
        benchmark_map = self.config.get("benchmark_map", {})
        ticker_upper = ticker.upper()
        for suffix, benchmark in benchmark_map.items():
            if suffix and ticker_upper.endswith(suffix.upper()):
                return benchmark
        return benchmark_map.get("", "SPY")

    def _fetch_returns(
        self, ticker: str, trade_date: str, holding_days: int = 5,
        benchmark: str = "SPY",
    ) -> tuple[float | None, float | None, int | None]:
        """trade_date부터 holding_days 동안의 원(raw) 수익률과 알파 수익률을 조회한다.

        ``benchmark``는 알파의 기준선(baseline)이 되는 지수입니다(호출자가
        ``_resolve_benchmark``로 결정). ``(raw_return, alpha_return,
        actual_holding_days)``를 반환하며, 가격 데이터를 구할 수 없으면
        (너무 최근이거나, 상장 폐지되었거나, 네트워크 오류)
        ``(None, None, None)``을 반환합니다.

        조기 확정 가드: 결정일 이후 실제 확보된 거래일 수가 ``holding_days``에
        못 미치면 (None, None, None)을 반환해 항목이 pending으로 남게 합니다.
        다음 날 재실행 시 5일 보유 의도가 1일 수익률(노이즈)로 영구 확정되던
        문제를 막고, 데이터가 충분히 쌓인 다음 실행에서 확정하게 합니다.
        """
        from tradingagents.dataflows.symbol_utils import normalize_symbol

        try:
            start = datetime.strptime(trade_date, "%Y-%m-%d")
            end = start + timedelta(days=holding_days + 7)  # 주말/휴장일 대비 여유분
            end_str = end.strftime("%Y-%m-%d")

            # 실현 수익률 조회가 분석 때 가격을 매긴 것과 동일한 종목을
            # 가리키도록 심볼을 정규화한다 (예: XAUUSD -> GC=F) (#984).
            # 벤치마크는 ``_resolve_benchmark``가 이미 표준 야후(Yahoo) 심볼로 준다.
            stock = yf.Ticker(normalize_symbol(ticker)).history(start=trade_date, end=end_str)
            bench = yf.Ticker(benchmark).history(start=trade_date, end=end_str)

            if len(stock) < 2 or len(bench) < 2:
                return None, None, None

            # 종목과 벤치마크 모두 보유 기간만큼의 거래일이 쌓였을 때만 확정.
            # 부족하면 pending으로 남겨 다음 실행에서 재시도한다.
            available_days = min(len(stock) - 1, len(bench) - 1)
            if available_days < holding_days:
                logger.info(
                    "Deferring outcome for %s on %s: only %d of %d trading days available",
                    ticker, trade_date, available_days, holding_days,
                )
                return None, None, None

            actual_days = holding_days
            raw = float(
                (stock["Close"].iloc[actual_days] - stock["Close"].iloc[0])
                / stock["Close"].iloc[0]
            )
            bench_ret = float(
                (bench["Close"].iloc[actual_days] - bench["Close"].iloc[0])
                / bench["Close"].iloc[0]
            )
            alpha = raw - bench_ret
            return raw, alpha, actual_days
        except Exception as e:
            logger.warning(
                "Could not resolve outcome for %s on %s vs %s (will retry next run): %s",
                ticker, trade_date, benchmark, e,
            )
            return None, None, None

    def _resolve_pending_entries(self, ticker: str) -> None:
        """새 실행 시작 시, 해당 티커의 결과 대기(pending) 로그 항목들을 해소한다.

        [초보자용 설명] 이전 실행에서 저장해 둔 매매 결정 중 아직 결과가
        확인되지 않은 항목들을 찾아, 실제 수익률을 조회하고 LLM 리플렉션
        (복기 요약)을 생성한 뒤 메모리 로그를 갱신합니다. 이렇게 쌓인 교훈이
        다음 분석에 과거 맥락으로 주입됩니다.

        같은 티커의 대기 항목별로 수익률을 조회하고 리플렉션을 생성한 다음,
        불필요한 I/O를 피하기 위해 모든 갱신을 한 번의 원자적(atomic) 일괄
        쓰기로 기록합니다. 가격 데이터가 아직 없는 항목(너무 최근이거나
        상장 폐지)은 건너뜁니다.

        트레이드오프: 실행당 같은 티커의 항목만 해소됩니다. 다른 티커의
        항목들은 그 티커가 다시 실행될 때까지 쌓입니다.
        """
        pending = [e for e in self.memory_log.get_pending_entries() if e["ticker"] == ticker]
        if not pending:
            return

        benchmark = self._resolve_benchmark(ticker)
        holding_days = self.config.get("holding_days", 5)
        updates = []
        for entry in pending:
            raw, alpha, days = self._fetch_returns(
                ticker, entry["date"], holding_days=holding_days, benchmark=benchmark,
            )
            if raw is None:
                continue  # 가격/거래일이 아직 부족함 — 다음 실행 때 다시 시도
            reflection = self.reflector.reflect_on_final_decision(
                final_decision=entry.get("decision", ""),
                raw_return=raw,
                alpha_return=alpha,
                benchmark_name=benchmark,
                actual_days=days,
                # 당시 investment_plan 요약(PLAN: 섹션). 반성이 "무엇을 근거로
                # 판단했는지"를 알 수 있게 한다. 구형 항목엔 없어 빈 문자열.
                investment_plan=entry.get("plan", ""),
            )
            updates.append({
                "ticker": ticker,
                "trade_date": entry["date"],
                "raw_return": raw,
                "alpha_return": alpha,
                "holding_days": days,
                "reflection": reflection,
            })

        if updates:
            self.memory_log.batch_update_with_outcomes(updates)

    def resolve_instrument_context(self, ticker: str, asset_type: str = "stock") -> str:
        """티커 정체성(identity)을 한 번만 조회해 전체 종목 컨텍스트 문자열을 반환한다.

        결정적(deterministic)인 yfinance 조회(캐시됨, 실패해도 계속 진행
        fail-open) 결과를 컨텍스트 문자열에 주입해서, 모든 에이전트가 가격
        차트만 보고 회사를 지어내는(hallucinate) 대신 실제 회사에 근거를
        두게 합니다 (#814). propagate() 경로와 CLI 둘 다 이 메서드를
        호출하므로, 어느 진입점으로 들어와도 해석된 정체성이 그래프 전체에
        전달됩니다.
        """
        identity = resolve_instrument_identity(ticker)
        return build_instrument_context(ticker, asset_type, identity)

    def _run_signature(self, asset_type: str) -> str:
        """변경 시 체크포인트를 무효화해야 하는, 그래프 모양에 영향을 주는 입력들.

        체크포인트 스레드 ID에 포함되어, 애널리스트 선택·토론/리스크 깊이·
        자산 모드가 달라진 상태로 재개(resume)하면 이전 그래프를 조용히
        이어가는 대신 처음부터 새로 시작하게 만듭니다 (#1089).
        """
        return "|".join([
            "analysts=" + ",".join(self.selected_analysts),
            f"debate={self.config['max_debate_rounds']}",
            f"risk={self.config['max_risk_discuss_rounds']}",
            f"asset={asset_type}",
        ])

    def propagate(self, company_name, trade_date, asset_type: str = "stock"):
        """특정 날짜의 특정 회사에 대해 트레이딩 에이전트 그래프를 실행한다.

        ``asset_type``은 주식 파이프라인(기본값)과 #567에서 추가된 암호화폐
        파이프라인(``"crypto"``) 중 하나를 선택합니다 — CLI는 티커에서 자동
        감지하고, 프로그래밍 방식 호출자는 명시적으로 전달합니다. 설정에서
        ``checkpoint_enabled``가 켜져 있으면 그래프를 티커별 SqliteSaver와
        함께 다시 컴파일하므로, 도중에 죽은 실행을 같은 티커+날짜로 다시
        호출하면 마지막으로 성공한 노드부터 재개할 수 있습니다.
        """
        self.ticker = company_name

        # 파이프라인 실행 전에 이 티커의 결과 대기(pending) 메모리 로그 항목들을 해소.
        self._resolve_pending_entries(company_name)

        # 사용자가 옵트인(opt-in)했다면 체크포인터와 함께 다시 컴파일.
        if self.config.get("checkpoint_enabled"):
            self._checkpointer_ctx = get_checkpointer(
                self.config["data_cache_dir"], company_name
            )
            saver = self._checkpointer_ctx.__enter__()
            self.graph = self.workflow.compile(checkpointer=saver)

            # 이전 실행이 남긴 체크포인트가 있는지 확인해 재개/새 시작 여부를 로깅.
            step = checkpoint_step(
                self.config["data_cache_dir"], company_name, str(trade_date),
                self._run_signature(asset_type),
            )
            if step is not None:
                logger.info(
                    "Resuming from step %d for %s on %s", step, company_name, trade_date
                )
            else:
                logger.info("Starting fresh for %s on %s", company_name, trade_date)

        try:
            return self._run_graph(company_name, trade_date, asset_type=asset_type)
        finally:
            # 성공/실패와 무관하게 체크포인터의 DB 연결을 닫고,
            # 그래프를 체크포인터 없는 상태로 되돌려 둔다.
            if self._checkpointer_ctx is not None:
                self._checkpointer_ctx.__exit__(None, None, None)
                self._checkpointer_ctx = None
                self.graph = self.workflow.compile()

    def save_reports(self, final_state, ticker, save_path=None) -> Path:
        """완료된 실행의 마크다운 보고서 트리를 CLI와 동일한 방식으로 기록한다.

        프로그래밍 방식 호출자도 CLI가 만드는 것과 같은 디스크 상의 보고서를
        얻습니다. 명시적으로 ``save_path``를 넘기거나, 기본값인
        ``results_dir`` 아래 경로를 사용하세요.
        """
        if save_path is None:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            save_path = (
                Path(self.config["results_dir"])
                / "reports"
                / f"{safe_ticker_component(ticker)}_{stamp}"
            )
        return write_report_tree(final_state, ticker, save_path)

    def _run_graph(self, company_name, trade_date, asset_type: str = "stock"):
        """그래프를 실행하고 결과 상태를 디스크와 메모리 로그에 기록한다."""
        # 상태 초기화 — 리서치 매니저·트레이더·포트폴리오 매니저(PM)용 메모리
        # 로그 컨텍스트(past_context)와, 모든 에이전트용으로 결정적으로 해석된
        # 종목 정체성을 함께 주입한다.
        past_context = self.memory_log.get_past_context(company_name, asset_type=asset_type)
        instrument_context = self.resolve_instrument_context(company_name, asset_type)
        init_agent_state = self.propagator.create_initial_state(
            company_name,
            trade_date,
            asset_type=asset_type,
            past_context=past_context,
            instrument_context=instrument_context,
        )
        args = self.propagator.get_graph_args()

        # thread_id를 주입해서 같은 티커+날짜+그래프 모양이면 재개하고,
        # 날짜나 그래프 모양이 다르면 새로 시작하게 한다 (#1089).
        if self.config.get("checkpoint_enabled"):
            tid = thread_id(company_name, str(trade_date), self._run_signature(asset_type))
            args.setdefault("config", {}).setdefault("configurable", {})["thread_id"] = tid

        if self.debug:
            # 디버그 모드: 그래프를 스트리밍하며 각 노드의 메시지를 화면에 출력.
            trace = []
            last_printed = None
            for chunk in self.graph.stream(init_agent_state, **args):
                if chunk["messages"]:
                    msg = chunk["messages"][-1]
                    # 트레이더 이후의 노드들은 messages에 추가하지 않으므로,
                    # 같은 마지막 메시지가 여러 청크(chunk)에 걸쳐 반복된다.
                    # 내용이 바뀌었을 때만 출력한다 (#1027). 트레이스/상태
                    # 병합 로직은 그대로다.
                    signature = (type(msg).__name__, getattr(msg, "content", None))
                    if signature != last_printed:
                        msg.pretty_print()
                        last_printed = signature
                    trace.append(chunk)
            # 스트리밍된 청크는 노드별 증분(delta)이다. 이를 병합해서
            # 비(非)디버그 경로의 graph.invoke()가 내놓는 상태와 같게 만든다.
            final_state = {}
            for chunk in trace:
                final_state.update(chunk)
        else:
            final_state = self.graph.invoke(init_agent_state, **args)

        # 리플렉션을 위해 현재 상태를 보관.
        self.curr_state = final_state

        # 상태를 디스크에 로깅.
        self._log_state(trade_date, final_state)

        # 다음번 같은 티커 실행 때 수행할 지연 리플렉션(deferred reflection)을
        # 위해 이번 결정을 저장.
        self.memory_log.store_decision(
            ticker=company_name,
            trade_date=trade_date,
            final_trade_decision=final_state["final_trade_decision"],
            asset_type=asset_type,
            # 당시 리서치 매니저의 투자 계획 앞부분(PLAN: 섹션)을 함께 저장해,
            # Phase B 반성이 결정의 근거를 보고 복기할 수 있게 한다.
            investment_plan=final_state.get("investment_plan", ""),
        )

        # 성공적으로 완료되면 체크포인트를 지워 오래된 상태가 남지 않게 한다.
        if self.config.get("checkpoint_enabled"):
            clear_checkpoint(
                self.config["data_cache_dir"], company_name, str(trade_date),
                self._run_signature(asset_type),
            )

        return final_state, self.process_signal(final_state["final_trade_decision"])

    def _log_state(self, trade_date, final_state):
        """최종 상태를 JSON 파일로 로깅한다."""
        self.log_states_dict[str(trade_date)] = {
            "company_of_interest": final_state["company_of_interest"],
            "trade_date": final_state["trade_date"],
            "market_report": final_state["market_report"],
            "sentiment_report": final_state["sentiment_report"],
            "news_report": final_state["news_report"],
            "fundamentals_report": final_state["fundamentals_report"],
            "investment_debate_state": {
                "bull_history": final_state["investment_debate_state"]["bull_history"],
                "bear_history": final_state["investment_debate_state"]["bear_history"],
                "history": final_state["investment_debate_state"]["history"],
                "current_response": final_state["investment_debate_state"][
                    "current_response"
                ],
                "judge_decision": final_state["investment_debate_state"][
                    "judge_decision"
                ],
            },
            "trader_investment_decision": final_state["trader_investment_plan"],
            "risk_debate_state": {
                "aggressive_history": final_state["risk_debate_state"]["aggressive_history"],
                "conservative_history": final_state["risk_debate_state"]["conservative_history"],
                "neutral_history": final_state["risk_debate_state"]["neutral_history"],
                "history": final_state["risk_debate_state"]["history"],
                "judge_decision": final_state["risk_debate_state"]["judge_decision"],
            },
            "investment_plan": final_state["investment_plan"],
            "final_trade_decision": final_state["final_trade_decision"],
        }

        # 파일로 저장. 경로 구성 요소로 합쳐졌을 때 결과 디렉터리를 벗어날 수
        # 있는 티커 값은 거부한다.
        safe_ticker = safe_ticker_component(self.ticker)
        directory = Path(self.config["results_dir"]) / safe_ticker / "TradingAgentsStrategy_logs"
        directory.mkdir(parents=True, exist_ok=True)

        log_path = directory / f"full_states_log_{trade_date}.json"
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(self.log_states_dict[str(trade_date)], f, indent=4)

    def process_signal(self, full_signal):
        """결정문(signal)을 처리해 핵심 결정(등급)만 추출한다."""
        return self.signal_processor.process_signal(full_signal)
