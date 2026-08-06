#!/usr/bin/env python3
# =============================================================================
# [스크립트 개요]
# 프롬프트 개선(작업이력 21) 약세·관망 쏠림 원인 분리 프로브.
#
# 배경: 백테스트-실험-결과.md (2026-08-06 추가 실험) — 21·22 반영 후 같은
# 상승장 구간에서 등급 분포가 강세 4%/Hold 54%/약세 42%로, 교정 후 기준선
# (50/42/8) 대비 크게 약세로 이동했다. 작업이력 21이 리서치 매니저(RM)
# 프롬프트에 넣은 두 문구가 유력 원인 후보다:
#   (a) 평가 지평(evaluation horizon) 문단 — get_horizon_instruction()
#   (b) priced-in 할인 문장 — 루브릭 1번의 "컨센서스 재진술은 약한 증거"
#
# 방법: bias_probe와 동일하게 저장된 상태 로그(토론 이력 포함)를 로드해
# **RM 판정만** 조건별로 재실행한다. 추가 백테스트 없이 원인을 분리한다.
#
# 조건 (모두 현행 build_research_manager_prompt 기반, 문자열 제거로 절제):
#   live         — 현행 운영 프롬프트 그대로 (지평 + 할인 포함)
#   no-horizon   — (a) 지평 문단만 제거
#   no-pricedin  — (b) 할인 문장만 제거
#   pre21        — 둘 다 제거 (= 작업이력 21 이전 corrected 프롬프트와 동등)
#
# 해석 가이드:
#   - 구(舊)토론 27표본에서 live가 pre21 대비 약세로 밀리면 → RM 문구가 원인.
#     no-horizon/no-pricedin 비교로 어느 문장인지 특정.
#   - 신(新)토론(rerun22) 24표본에서 pre21이 여전히 약세로 밀리면 →
#     리서처 토론 내용 변화(bull/bear의 priced-in·지평 문구)도 기여.
#
# 실행 예:
#   TRADINGAGENTS_LLM_PROVIDER=bedrock \
#   TRADINGAGENTS_DEEP_THINK_LLM=us.anthropic.claude-sonnet-4-5-20250929-v1:0 \
#   AWS_DEFAULT_REGION=us-east-1 \
#   .venv/bin/python scripts/prompt_ablation_probe.py \
#     --old-root ~/.tradingagents/logs/backtest \
#     --new-roots ~/.tradingagents/logs/backtest/rerun22_MSFT,...
# =============================================================================
"""RM 프롬프트 절제(ablation) 프로브: 지평/priced-in 문구의 등급 분포 기여 분리."""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as _dt
import functools
import json
import socket
import sys
import time
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(_REPO_ROOT), str(_REPO_ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from bias_probe import (  # noqa: E402 — 형식 지시·표본 로더·요약기를 재사용
    CORRECTED_FORMAT_TEMPLATE,
    load_samples,
)
from tradingagents.agents.managers.research_manager import (  # noqa: E402
    build_research_manager_prompt,
)
from tradingagents.agents.utils.agent_utils import get_horizon_instruction  # noqa: E402
from tradingagents.agents.utils.rating import RATINGS_5_TIER, parse_rating  # noqa: E402
from tradingagents.agents.utils.structured import RATING_LINE_INSTRUCTION  # noqa: E402

CONDITIONS = ("live", "no-horizon", "no-pricedin", "pre21")

# 작업이력 21에서 RM 루브릭 1번에 추가된 priced-in 할인 문장 (원문 그대로 —
# research_manager.py와 어긋나면 아래 assert가 실행 시점에 잡는다).
PRICEDIN_SENTENCE = (
    " Weigh new information and variant views against market expectations more "
    "heavily than recitations of facts the market has long known and priced — in "
    "either direction, restating consensus is weak evidence for a directional rating."
)


def build_ablated_prompt(state: dict[str, Any], condition: str) -> str:
    """현행 RM 프롬프트에서 조건에 따라 지평/할인 문구를 제거해 반환한다."""
    prompt = build_research_manager_prompt(state)
    horizon = get_horizon_instruction()
    # 절제 대상이 실제로 존재하는지 확인 — 운영 프롬프트가 바뀌어 문자열이
    # 어긋나면 조용히 무의미한 실험이 되는 것을 막는다.
    assert horizon in prompt, "지평 문단이 RM 프롬프트에 없음 — 문자열 불일치"
    assert PRICEDIN_SENTENCE in prompt, "priced-in 문장이 RM 프롬프트에 없음 — 문자열 불일치"
    if condition in ("no-horizon", "pre21"):
        prompt = prompt.replace(horizon, "")
    if condition in ("no-pricedin", "pre21"):
        prompt = prompt.replace(PRICEDIN_SENTENCE, "")
    return prompt + CORRECTED_FORMAT_TEMPLATE + RATING_LINE_INSTRUCTION


def _call(llm: Any, sample: dict[str, Any], condition: str) -> dict[str, Any]:
    row: dict[str, Any] = {
        "ticker": sample["ticker"], "date": sample["date"],
        "debates": sample["debates"], "condition": condition,
    }
    try:
        prompt = build_ablated_prompt(sample["state"], condition)
        row["prompt_chars"] = len(prompt)
        t0 = time.monotonic()
        response = llm.invoke(prompt)
        row["elapsed_s"] = round(time.monotonic() - t0, 1)
        text = response.content if isinstance(response.content, str) else str(response.content)
        row["decision"] = text
        row["rating"] = parse_rating(text, context=f"ablation:{condition}")
    except Exception as exc:  # noqa: BLE001 — 실패 표본 격리
        row["error"] = f"{type(exc).__name__}: {exc}"
    return row


BULLISH = {"Buy", "Overweight"}
BEARISH = {"Underweight", "Sell"}


def summarize(rows: list[dict[str, Any]]) -> str:
    lines = [
        "# prompt_ablation_probe 결과 요약",
        "",
        f"생성 시각: {_dt.datetime.now().isoformat(timespec='seconds')}",
        "",
        "| 토론 | 조건 | n | Buy | OW | Hold | UW | Sell | 강세% | Hold% | 약세% | 실패 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    keys = sorted({(r["debates"], r["condition"]) for r in rows},
                  key=lambda k: (k[0], CONDITIONS.index(k[1])))
    for debates, cond in keys:
        sub = [r for r in rows if r["debates"] == debates and r["condition"] == cond]
        ok = [r for r in sub if "rating" in r]
        n = len(ok)
        c = {t: sum(1 for r in ok if r["rating"] == t) for t in RATINGS_5_TIER}
        bull = sum(c[t] for t in BULLISH); bear = sum(c[t] for t in BEARISH)
        pct = (lambda x: f"{100*x/n:.0f}%" if n else "-")
        lines.append(
            f"| {debates} | {cond} | {n} | {c['Buy']} | {c['Overweight']} | {c['Hold']} | "
            f"{c['Underweight']} | {c['Sell']} | {pct(bull)} | {pct(c['Hold'])} | {pct(bear)} | "
            f"{len(sub) - n} |"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="RM 프롬프트 절제 프로브 (작업이력 21 원인 분리)")
    parser.add_argument("--old-root", default=str(Path.home() / ".tradingagents/logs/backtest"),
                        help="구토론 상태 로그 루트 (원본 27표본)")
    parser.add_argument("--new-roots", default="",
                        help="신토론(rerun22) 루트들 — 쉼표 구분, 각 루트 아래 티커 디렉터리")
    parser.add_argument("--old-conditions", default="live,no-horizon,no-pricedin,pre21")
    parser.add_argument("--new-conditions", default="pre21",
                        help="신토론에 적용할 조건 (live×신토론은 백테스트 관측치로 갈음)")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--max-calls", type=int, default=150)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    socket.setdefaulttimeout(120)

    jobs: list[tuple[dict[str, Any], str]] = []
    old_samples = load_samples(Path(args.old_root).expanduser())
    for s in old_samples[: args.limit]:
        s["debates"] = "old"
        for c in [c.strip() for c in args.old_conditions.split(",") if c.strip()]:
            jobs.append((s, c))
    for root in [r.strip() for r in args.new_roots.split(",") if r.strip()]:
        new_samples = load_samples(Path(root).expanduser())
        for s in new_samples[: args.limit]:
            s["debates"] = "new"
            for c in [c.strip() for c in args.new_conditions.split(",") if c.strip()]:
                jobs.append((s, c))

    print(f"총 호출 수: {len(jobs)} (구토론 {len(old_samples)}표본)")
    if len(jobs) > args.max_calls:
        print(f"오류: 상한 {args.max_calls} 초과"); return 1
    if not jobs:
        print("오류: 표본 없음"); return 1

    from tradingagents.dataflows.config import get_config
    from tradingagents.llm_clients import create_llm_client
    config = get_config()
    llm_kwargs: dict[str, Any] = {}
    if config.get("temperature") not in (None, ""):
        llm_kwargs["temperature"] = float(config["temperature"])
    client = create_llm_client(
        provider=config["llm_provider"],
        model=config["deep_think_llm"],
        base_url=config.get("backend_url"),
        **llm_kwargs,
    )
    llm = client.get_llm()  # LangChain 인터페이스(.invoke)로 감싼 LLM

    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out) if args.out else (
        Path.home() / ".tradingagents/logs/bias_probe" / f"ablation_{ts}")
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"출력: {out_dir}")

    rows: list[dict[str, Any]] = []
    with open(out_dir / "rows.jsonl", "a", encoding="utf-8") as f:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(_call, llm, s, c) for s, c in jobs]
            for i, fut in enumerate(concurrent.futures.as_completed(futures), 1):
                row = fut.result()
                f.write(json.dumps(row, ensure_ascii=False) + "\n"); f.flush()
                rows.append(row)
                print(f"[{i}/{len(jobs)}] {row['debates']}/{row['ticker']} {row['date']} "
                      f"{row['condition']} = {row.get('rating', 'ERR')}", flush=True)

    summary = summarize(rows)
    (out_dir / "summary.md").write_text(summary, encoding="utf-8")
    print("\n" + summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
