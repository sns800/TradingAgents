# =============================================================================
# [모듈 개요 — 초보자용]
# 이 스크립트는 메모리 로그(TradingMemoryLog)에 쌓인 해소(resolved)된 매매
# 결정들을 집계해 스코어보드를 출력하는 CLI입니다. 집계 로직 자체는
# tradingagents/eval/scoreboard.py에 있고, 여기는 인자 파싱과 입출력만
# 담당합니다. LLM·네트워크 호출이 전혀 없으므로 언제든 무료로 실행할 수
# 있습니다.
#
# 사용법:
#   python scripts/scoreboard.py
#   python scripts/scoreboard.py --memory-log ~/.tradingagents/memory/trading_memory.md
#   python scripts/scoreboard.py --hold-threshold 0.02 --output scoreboard.md
# =============================================================================

"""메모리 로그 스코어보드 CLI — 등급별 방향 적중률·평균 알파·베이스라인 비교."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tradingagents.agents.utils.memory import TradingMemoryLog
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.eval.scoreboard import (
    DEFAULT_HOLD_THRESHOLD,
    aggregate_entries,
    render_markdown,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "메모리 로그의 해소된(resolved) 매매 결정을 집계해 등급별 방향 "
            "적중률·평균 원수익률·평균 알파를 마크다운 표로 출력합니다. "
            "항상-Hold·랜덤 베이스라인 대비 기대 적중률도 함께 보여 줍니다."
        ),
    )
    parser.add_argument(
        "--memory-log",
        default=None,
        help="메모리 로그 파일 경로 (기본: 설정의 memory_log_path)",
    )
    parser.add_argument(
        "--hold-threshold",
        type=float,
        default=DEFAULT_HOLD_THRESHOLD,
        help="Hold 적중 판정용 |알파| 임계값, 소수 비율 (기본: 0.01 = 1%%)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="마크다운 표를 저장할 파일 경로 (선택, 표준 출력에는 항상 표시)",
    )
    args = parser.parse_args()

    log_path = args.memory_log or DEFAULT_CONFIG["memory_log_path"]
    if not Path(log_path).expanduser().exists():
        print(f"Memory log not found: {log_path}", file=sys.stderr)
        return 1

    memory_log = TradingMemoryLog({"memory_log_path": log_path})
    entries = memory_log.load_entries()
    summary = aggregate_entries(entries, hold_threshold=args.hold_threshold)
    markdown = render_markdown(summary)

    print(markdown)
    if args.output:
        output_path = Path(args.output).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(markdown + "\n", encoding="utf-8")
        print(f"\nSaved scoreboard to {output_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
