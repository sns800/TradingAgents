# =============================================================================
# [모듈 개요 — 초보자용]
# 이 파일은 일괄 백테스트의 순수 로직입니다. scripts/backtest.py는 이 모듈을
# 호출하는 얇은 CLI일 뿐입니다. 주요 구성:
#   - build_schedule():          (티커, 날짜) 실행 조합 생성 (거래일 간격)
#   - run_backtest():            각 조합에 대해 결정 함수를 실행, 실패 격리
#   - compute_holding_returns(): 복수 보유기간(1/5/20 거래일) 수익률·알파 계산
#   - annotate_returns():        결정 레코드에 수익률을 사후 병기
#   - summarize_records():       모드별·등급별 적중률/평균 알파 마크다운 표
#
# 룩어헤드 주의: 파이프라인 실행 자체의 데이터 경로들은 curr_date 필터를
# 지원하며(단기 로드맵에서 강화됨), 이 모듈은 그 위에서 도는 소비자입니다.
# 여기서 조회하는 미래 가격은 "결정 이후"의 수익률을 사후 채점하는 용도로만
# 쓰이고, 결정 생성 경로에는 절대 주입되지 않습니다.
# =============================================================================

"""일괄 백테스트 러너 — 과거 날짜 범위 실행, 복수 보유기간 수익률, 요약 표."""

from __future__ import annotations

import copy
import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from tradingagents.agents.utils.rating import RATINGS_5_TIER
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.eval.scoreboard import DEFAULT_HOLD_THRESHOLD, is_directional_hit

logger = logging.getLogger(__name__)

# 사후 채점에 사용하는 보유기간(거래일 수) 집합
HOLDING_HORIZONS = (1, 5, 20)
# JSONL에 저장하는 결정문 본문의 최대 문자 수
DECISION_SNIPPET_CHARS = 2000

# 결정 함수 타입: (ticker, trade_date) -> {"rating": str, "decision": str}
DecisionFn = Callable[[str, str], dict]


def build_schedule(
    tickers: list[str], start: str, end: str, every: int = 1
) -> list[tuple[str, str]]:
    """(티커, 날짜) 실행 조합을 시간순으로 생성한다.

    날짜는 ``start``~``end`` 사이의 영업일(business day)을 ``every`` 간격으로
    추립니다. (영업일은 주말만 제외한 근사치로, 거래소 휴장일이 섞여도
    해당 날짜의 파이프라인은 그 시점까지의 데이터로 정상 동작하며 수익률
    채점은 실제 거래일 종가 기준이므로 무해합니다.)

    같은 날짜의 티커들을 묶어 날짜 오름차순으로 배치합니다 — 메모리 학습이
    켜진 실행에서 과거 결정이 미래 결정보다 먼저 해소되도록 하기 위함입니다.
    """
    if every < 1:
        raise ValueError(f"every must be >= 1, got {every}")
    dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range(start=start, end=end)[::every]]
    return [(ticker, date) for date in dates for ticker in tickers]


def resolve_benchmark(ticker: str, config: dict | None = None) -> str:
    """티커의 알파 계산용 벤치마크를 config 규칙으로 결정한다.

    ``benchmark_ticker``가 설정돼 있으면 항상 우선하고, 아니면 거래소 접미사
    매핑(``benchmark_map``)을 따르며, 기본값은 SPY입니다.
    (trading_graph._resolve_benchmark와 동일한 규칙의 독립 구현 — 그래프
    인스턴스 없이도 채점할 수 있게 합니다.)
    """
    cfg = config or DEFAULT_CONFIG
    explicit = cfg.get("benchmark_ticker")
    if explicit:
        return explicit
    benchmark_map = cfg.get("benchmark_map", {})
    ticker_upper = ticker.upper()
    for suffix, benchmark in benchmark_map.items():
        if suffix and ticker_upper.endswith(suffix.upper()):
            return benchmark
    return benchmark_map.get("", "SPY")


def default_fetch_history(symbol: str, start: str, end: str):
    """야후 파이낸스에서 종가 시계열을 조회한다(기본 가격 조회 구현).

    테스트에서는 이 함수 대신 결정론적 시계열을 반환하는 가짜 함수를
    주입합니다. 심볼 정규화는 분석 시점과 같은 종목을 가리키도록
    trading_graph._fetch_returns와 동일하게 적용합니다.
    """
    import yfinance as yf

    from tradingagents.dataflows.symbol_utils import normalize_symbol

    return yf.Ticker(normalize_symbol(symbol)).history(start=start, end=end)["Close"]


def compute_holding_returns(
    ticker: str,
    trade_date: str,
    benchmark: str = "SPY",
    horizons: tuple[int, ...] = HOLDING_HORIZONS,
    fetch_history: Callable | None = None,
) -> dict:
    """결정일 이후 복수 보유기간의 원수익률과 벤치마크 대비 알파를 계산한다.

    반환: {"1d": {"raw": float|None, "alpha": float|None}, "5d": ..., ...}
    보유기간만큼의 거래일이 아직 쌓이지 않았거나(너무 최근) 가격 조회가
    실패하면 해당 보유기간의 값은 None으로 남습니다(예외를 던지지 않음).

    수익률 기준점: ``trade_date``의 종가(첫 거래일 종가)이며, h 거래일 뒤
    종가와의 변화율입니다 — trading_graph._fetch_returns와 같은 규약입니다.
    """
    fetch = fetch_history or default_fetch_history
    result: dict[str, dict] = {f"{h}d": {"raw": None, "alpha": None} for h in horizons}

    start_dt = datetime.strptime(trade_date, "%Y-%m-%d")
    # 최장 보유기간(거래일)을 달력일로 넉넉히 환산 + 휴장일 여유분
    end_str = (start_dt + timedelta(days=max(horizons) * 2 + 10)).strftime("%Y-%m-%d")

    try:
        stock = fetch(ticker, trade_date, end_str)
        bench = fetch(benchmark, trade_date, end_str)
    except Exception as e:
        logger.warning(
            "Could not fetch prices for %s on %s vs %s: %s", ticker, trade_date, benchmark, e
        )
        return result

    for h in horizons:
        # 인덱스 0(결정일 종가)에 더해 h개의 거래일 종가가 양쪽 모두 필요
        if len(stock) <= h or len(bench) <= h:
            continue
        base = float(stock.iloc[0])
        bench_base = float(bench.iloc[0])
        if base == 0 or bench_base == 0:
            continue
        raw = float(stock.iloc[h]) / base - 1.0
        bench_ret = float(bench.iloc[h]) / bench_base - 1.0
        result[f"{h}d"] = {"raw": raw, "alpha": raw - bench_ret}
    return result


def make_decision_fn(mode: str, config: dict | None = None, depth: int = 1) -> DecisionFn:
    """모드에 맞는 결정 함수를 만든다.

    - "full":       전체 멀티 에이전트 파이프라인(TradingAgentsGraph.propagate)
    - "single_llm": 단일 LLM 1회 호출 베이스라인(eval.baseline)

    ``depth``는 full 모드의 토론·리스크 라운드 수를 함께 덮어씁니다.
    무거운 의존성(LLM SDK, 그래프)은 이 함수 안에서 지연 임포트되므로,
    테스트는 가짜 결정 함수를 직접 주입해 이 함수를 우회할 수 있습니다.
    """
    cfg = copy.deepcopy(config or DEFAULT_CONFIG)
    cfg["max_debate_rounds"] = depth
    cfg["max_risk_discuss_rounds"] = depth

    if mode == "full":
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        graph = TradingAgentsGraph(config=cfg)

        def fn(ticker: str, trade_date: str) -> dict:
            final_state, rating = graph.propagate(ticker, trade_date)
            return {
                "rating": rating,
                "decision": final_state.get("final_trade_decision", ""),
            }

        return fn

    if mode == "single_llm":
        from tradingagents.eval.baseline import create_baseline_llm, run_single_llm_baseline

        llm = create_baseline_llm(cfg)  # 조합마다 재생성하지 않도록 한 번만 생성

        def fn(ticker: str, trade_date: str) -> dict:
            out = run_single_llm_baseline(ticker, trade_date, config=cfg, llm=llm)
            return {"rating": out["rating"], "decision": out["response"]}

        return fn

    raise ValueError(f"Unknown backtest mode: {mode!r} (expected 'full' or 'single_llm')")


def run_backtest(
    schedule: list[tuple[str, str]], decision_fn: DecisionFn, mode: str = "full"
) -> list[dict]:
    """스케줄의 각 (티커, 날짜)에 대해 결정 함수를 실행하고 레코드를 만든다.

    개별 조합의 실패는 해당 레코드에 오류로 기록하고 다음 조합을 계속
    실행합니다(실패 격리) — 긴 배치가 한 번의 API 오류로 통째로 죽지 않게.
    """
    records = []
    total = len(schedule)
    for i, (ticker, trade_date) in enumerate(schedule, start=1):
        logger.info("Backtest run %d/%d: %s on %s", i, total, ticker, trade_date)
        record = {"ticker": ticker, "trade_date": trade_date, "mode": mode}
        try:
            out = decision_fn(ticker, trade_date)
            record["rating"] = out["rating"]
            record["decision"] = (out.get("decision") or "")[:DECISION_SNIPPET_CHARS]
            record["status"] = "ok"
        except Exception as e:
            logger.exception("Backtest run failed for %s on %s", ticker, trade_date)
            record["status"] = "error"
            record["error"] = f"{type(e).__name__}: {e}"
        records.append(record)
    return records


def annotate_returns(
    records: list[dict],
    config: dict | None = None,
    horizons: tuple[int, ...] = HOLDING_HORIZONS,
    fetch_history: Callable | None = None,
) -> list[dict]:
    """성공한 결정 레코드에 복수 보유기간 수익률·알파를 사후 병기한다.

    오류 레코드는 건너뜁니다. 레코드를 제자리(in-place)에서 갱신하고
    같은 리스트를 반환합니다.
    """
    for record in records:
        if record.get("status") != "ok":
            continue
        benchmark = resolve_benchmark(record["ticker"], config)
        record["benchmark"] = benchmark
        record["returns"] = compute_holding_returns(
            record["ticker"], record["trade_date"], benchmark, horizons, fetch_history
        )
    return records


def write_jsonl(records: list[dict], path: str | Path) -> Path:
    """레코드들을 JSONL(줄 단위 JSON) 파일로 기록한다."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def summarize_records(
    records: list[dict],
    hold_threshold: float = DEFAULT_HOLD_THRESHOLD,
    primary_horizon: int = 5,
    horizons: tuple[int, ...] = HOLDING_HORIZONS,
) -> str:
    """모드별·등급별 적중률과 보유기간별 평균 알파를 마크다운 표로 요약한다.

    방향 적중은 ``primary_horizon``(기본 5거래일) 알파를 기준으로 판정합니다.
    수익률이 아직 없는(None) 항목은 적중률 분모에서 제외됩니다.
    """
    key = f"{primary_horizon}d"
    groups: dict[tuple[str, str], list[dict]] = {}
    error_count = 0
    for record in records:
        if record.get("status") != "ok":
            error_count += 1
            continue
        groups.setdefault((record["mode"], record.get("rating", "Hold")), []).append(record)

    horizon_keys = [f"{h}d" for h in horizons]
    header_alpha = " | ".join(f"평균 알파 {k}" for k in horizon_keys)
    lines = [
        "# 백테스트 요약",
        "",
        f"- 총 실행: {len(records)}건 (실패: {error_count}건)",
        f"- 방향 적중 판정 기준: {key} 알파, Hold 임계값 |알파| < {hold_threshold:.2%}",
        "",
        f"| 모드 | 등급 | 건수 | 적중률 ({key}) | {header_alpha} |",
        "|---|---|---:|---:|" + "---:|" * len(horizon_keys),
    ]

    def _rating_order(rating: str) -> int:
        return RATINGS_5_TIER.index(rating) if rating in RATINGS_5_TIER else len(RATINGS_5_TIER)

    for (mode, rating), recs in sorted(
        groups.items(), key=lambda kv: (kv[0][0], _rating_order(kv[0][1]))
    ):
        alphas_primary = [
            r["returns"][key]["alpha"]
            for r in recs
            if r.get("returns", {}).get(key, {}).get("alpha") is not None
        ]
        hits = sum(
            1 for a in alphas_primary if is_directional_hit(rating, a, hold_threshold)
        )
        hit_rate = f"{hits / len(alphas_primary):.1%}" if alphas_primary else "n/a"

        alpha_cells = []
        for hk in horizon_keys:
            alphas = [
                r["returns"][hk]["alpha"]
                for r in recs
                if r.get("returns", {}).get(hk, {}).get("alpha") is not None
            ]
            alpha_cells.append(
                f"{sum(alphas) / len(alphas):+.2%}" if alphas else "n/a"
            )
        lines.append(
            f"| {mode} | {rating} | {len(recs)} | {hit_rate} | " + " | ".join(alpha_cells) + " |"
        )

    if not groups:
        lines.append("| (표본 없음) | | 0 | n/a |" + " n/a |" * len(horizon_keys))
    return "\n".join(lines)
