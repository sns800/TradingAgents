# =============================================================================
# [모듈 개요 — 초보자용]
# 이 스크립트는 과거 날짜 범위에 대해 파이프라인(또는 단일 LLM 베이스라인)을
# 일괄 실행하는 백테스트 CLI입니다. 실행 로직은 tradingagents/eval/backtest.py에
# 있고, 여기는 인자 파싱·설정 조립·입출력만 담당합니다.
#
# 동작 순서:
#   1. --tickers × (--start ~ --end 사이 --every 거래일 간격 날짜)의 조합 생성
#   2. 각 조합에 대해 결정 실행 (--mode full: 전체 파이프라인 / single_llm:
#      단일 LLM 베이스라인). 개별 실패는 기록만 하고 계속 진행
#   3. 각 결정에 대해 1/5/20 거래일 수익률과 벤치마크 대비 알파를 야후
#      데이터로 사후 계산해 JSONL에 병기
#   4. 모드별·등급별 적중률/평균 알파 요약 표 출력
#
# 비용 주의: --mode full은 조합당 전체 멀티 에이전트 그래프를 실행하므로
# LLM API 비용이 큽니다. 표본 수(티커 수 × 날짜 수)를 먼저 계산해 보세요.
#
# 격리: full 모드의 메모리 로그는 --results-dir 아래의 backtest_memory.md를
# 사용합니다 — 백테스트가 운영 메모리 로그를 오염시키지 않게 하기 위함입니다.
#
# 사용법:
#   python scripts/backtest.py --tickers SPY,AAPL --start 2025-01-06 \
#       --end 2025-03-31 --every 5 --depth 1 --mode full
#   python scripts/backtest.py --tickers AAPL --start 2025-01-06 \
#       --end 2025-01-31 --mode single_llm
# =============================================================================

"""일괄 백테스트 CLI — 과거 범위 실행, 복수 보유기간 채점, 모드별 요약."""

from __future__ import annotations

import argparse
import copy
import logging
import socket
import sys
from datetime import datetime
from pathlib import Path

# 일부 데이터 라이브러리(yfinance 등)의 내부 HTTP 호출에는 타임아웃이 없어,
# 응답 없는 소켓 읽기에서 백테스트 전체가 무한 대기할 수 있다 (실측: 30분+ 정지).
# 전역 소켓 기본 타임아웃을 걸면 그런 호출이 예외로 바뀌고, run_backtest의
# 조합별 실패 격리가 다음 조합으로 진행시킨다. 명시적 타임아웃이 있는 호출
# (requests/botocore)에는 영향이 없다.
socket.setdefaulttimeout(120)

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.eval.backtest import (
    annotate_returns,
    build_schedule,
    make_decision_fn,
    run_backtest,
    summarize_records,
    write_jsonl,
)
from tradingagents.eval.scoreboard import DEFAULT_HOLD_THRESHOLD

logger = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "과거 날짜 범위에 대해 (티커 × 날짜) 조합을 일괄 실행하고, 결정마다 "
            "1/5/20 거래일 수익률·알파를 사후 계산해 JSONL로 기록한 뒤 모드별·"
            "등급별 요약 표를 출력합니다. --mode full은 전체 멀티 에이전트 "
            "파이프라인, single_llm은 단일 LLM 1회 호출 베이스라인입니다."
        ),
    )
    parser.add_argument(
        "--tickers", required=True,
        help="쉼표로 구분한 티커 목록 (예: SPY,AAPL)",
    )
    parser.add_argument("--start", required=True, help="시작일 (yyyy-mm-dd)")
    parser.add_argument("--end", required=True, help="종료일 (yyyy-mm-dd)")
    parser.add_argument(
        "--every", type=int, default=5,
        help="실행 날짜의 거래일 간격 (기본: 5 — 주 1회)",
    )
    parser.add_argument(
        "--depth", type=int, default=1,
        help="full 모드의 토론·리스크 라운드 수 (기본: 1)",
    )
    parser.add_argument(
        "--mode", choices=("full", "single_llm"), default="full",
        help="full: 전체 파이프라인 / single_llm: 단일 LLM 베이스라인 (기본: full)",
    )
    parser.add_argument(
        "--results-dir", default=None,
        help="JSONL·보고서·백테스트 전용 메모리 로그를 저장할 디렉터리 "
             "(기본: 설정 results_dir 아래 backtest/)",
    )
    parser.add_argument(
        "--hold-threshold", type=float, default=DEFAULT_HOLD_THRESHOLD,
        help="Hold 적중 판정용 |알파| 임계값, 소수 비율 (기본: 0.01 = 1%%)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    if not tickers:
        parser.error("--tickers must contain at least one ticker")

    results_dir = Path(
        args.results_dir or Path(DEFAULT_CONFIG["results_dir"]) / "backtest"
    ).expanduser()
    results_dir.mkdir(parents=True, exist_ok=True)

    # 백테스트 전용 설정: 결과·메모리 로그를 results_dir 아래로 격리해
    # 운영 메모리 로그(교훈 저장소)가 백테스트 결정으로 오염되지 않게 한다.
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["results_dir"] = str(results_dir)
    config["memory_log_path"] = str(results_dir / "backtest_memory.md")

    schedule = build_schedule(tickers, args.start, args.end, every=args.every)
    logger.info(
        "Backtest schedule: %d runs (%d tickers x %d dates), mode=%s",
        len(schedule), len(tickers), len(schedule) // max(len(tickers), 1), args.mode,
    )
    if not schedule:
        print("No trading dates in the given range.", file=sys.stderr)
        return 1

    decision_fn = make_decision_fn(args.mode, config=config, depth=args.depth)
    records = run_backtest(schedule, decision_fn, mode=args.mode)

    # 결정 이후 구간의 가격으로 사후 채점 (결정 생성 경로와 완전히 분리됨)
    annotate_returns(records, config=config)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    jsonl_path = write_jsonl(records, results_dir / f"backtest_{args.mode}_{stamp}.jsonl")
    logger.info("Wrote %d records to %s", len(records), jsonl_path)

    print()
    print(summarize_records(records, hold_threshold=args.hold_threshold))
    print(f"\nJSONL: {jsonl_path}")

    failures = sum(1 for r in records if r.get("status") != "ok")
    return 0 if failures < len(records) else 1


if __name__ == "__main__":
    sys.exit(main())
