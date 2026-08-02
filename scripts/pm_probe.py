#!/usr/bin/env python3
# =============================================================================
# [스크립트 개요]
# 포트폴리오 매니저(PM) 리스크 감독 게이트 재판정 프로브 (BACKLOG.md B2 옵션 b).
#
# 배경: 전수 조사(BACKLOG.md B2, 편향검증-실험-결과.md)에서 리서치 매니저(RM)
# → PM 최종 등급 밴드 변경이 0/40으로 확인됐다. 리스크 토론 3인 + PM이
# 독립적인 분석 텍스트는 생성하지만 등급 밴드는 한 번도 RM과 다르게 내지
# 않는 "고무도장" 구조였다. 이를 교정하기 위해 PM을 재종합자에서 리스크 감독
# 게이트로 재정의(portfolio_manager.build_portfolio_manager_prompt)했다.
#
# 이 프로브의 목적: 재프레이밍이 실제로 밴드를 움직이는지 저비용으로 측정한다.
# 저장된 상태 로그 40개(backtest 27 + backtest_phase3 13)를 재활용해 **PM만**
# 새 프롬프트로 1회 재판정하고, 최종 등급이 RM과 다른 비율(override율)과
# 방향(하향/상향)을 집계한다. 전체 파이프라인 재실행 비용의 1/N로 인과를
# 분리하는 편향검증 실험(scripts/bias_probe.py)과 동일한 재판정(rejudge) 방식.
#
# 목표 범위: override율 15~45% (0%면 재프레이밍 실패, >55%면 과잉).
# 방향은 하향(downgrade)에 기울어야 정상 — 자본 보호 비대칭 기준의 의도된 결과.
#
# 실행 예:
#   TRADINGAGENTS_LLM_PROVIDER=bedrock \
#   TRADINGAGENTS_DEEP_THINK_LLM=us.anthropic.claude-sonnet-4-5-20250929-v1:0 \
#   AWS_DEFAULT_REGION=us-east-1 \
#   .venv/bin/python scripts/pm_probe.py
#
#   .venv/bin/python scripts/pm_probe.py --dry-run --limit 2   # 프롬프트만 생성
# =============================================================================
"""포트폴리오 매니저 재판정 프로브: 리스크 감독 게이트 재프레이밍의 override율 측정."""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as _dt
import json
import socket
import sys
import time
from pathlib import Path
from typing import Any

# 스크립트를 저장소 루트 밖에서 실행해도 tradingagents 패키지를 찾을 수 있게 한다.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tradingagents.agents.managers.portfolio_manager import (  # noqa: E402
    build_portfolio_manager_prompt,
)
from tradingagents.agents.schemas import PortfolioDecision  # noqa: E402
from tradingagents.agents.utils.rating import RATINGS_5_TIER, parse_rating  # noqa: E402
from tradingagents.agents.utils.structured import (  # noqa: E402
    RATING_LINE_INSTRUCTION,
    bind_structured,
)

# 기본 상태 로그 루트 2곳 (편향검증 Phase 3 이전 27 + Phase 3 교정 후 13 = 40).
DEFAULT_LOG_ROOTS = (
    Path.home() / ".tradingagents" / "logs" / "backtest",
    Path.home() / ".tradingagents" / "logs" / "backtest_phase3",
)

BULLISH = {"Buy", "Overweight"}
BEARISH = {"Underweight", "Sell"}


# ---------------------------------------------------------------------------
# 표본 로드 · 상태 정규화
# ---------------------------------------------------------------------------


def load_samples(
    log_roots: list[Path], tickers: list[str] | None = None
) -> list[dict[str, Any]]:
    """여러 백테스트 상태 로그 루트를 모두 로드해 표본 리스트로 만든다.

    루트 이름을 표본 태그에 붙여(backtest / backtest_phase3) 두 체제의 표본을
    구분할 수 있게 한다.
    """
    samples = []
    for log_root in log_roots:
        if not log_root.is_dir():
            continue
        root_tag = log_root.name
        ticker_dirs = sorted(
            d for d in log_root.iterdir()
            if d.is_dir() and (tickers is None or d.name in tickers)
        )
        for ticker_dir in ticker_dirs:
            state_dir = ticker_dir / "TradingAgentsStrategy_logs"
            if not state_dir.is_dir():
                continue
            for path in sorted(state_dir.glob("full_states_log_*.json")):
                date = path.stem.replace("full_states_log_", "")
                with open(path, encoding="utf-8") as f:
                    state = json.load(f)
                samples.append({
                    "root": root_tag,
                    "ticker": ticker_dir.name,
                    "date": date,
                    "state": normalize_state(state),
                })
    return samples


def normalize_state(state: dict[str, Any]) -> dict[str, Any]:
    """저장된 상태 로그를 PM 프롬프트 빌더가 기대하는 키로 정규화한다.

    저장 로그는 트레이더 산출물을 ``trader_investment_decision``으로 직렬화하지만
    라이브 PM 노드(build_portfolio_manager_prompt)는 ``trader_investment_plan``을
    읽는다. 프로브가 실제 프롬프트 빌더를 그대로 쓰도록 여기서 키를 맞춘다.
    """
    if "trader_investment_plan" not in state:
        state = {
            **state,
            "trader_investment_plan": state.get("trader_investment_decision", ""),
        }
    return state


# ---------------------------------------------------------------------------
# override 분류 (밴드 이동 방향)
# ---------------------------------------------------------------------------


def classify_override(rm_rating: str, final_rating: str) -> str:
    """RM 등급 대비 최종 등급의 밴드 이동을 confirm/upgrade/downgrade로 분류한다.

    RATINGS_5_TIER는 가장 강세(Buy)에서 가장 약세(Sell) 순이므로, 인덱스가
    커질수록 약세다. 최종 인덱스가 RM보다 크면 하향(downgrade, 자본 보호 방향),
    작으면 상향(upgrade), 같으면 확정(confirm).
    """
    ri = RATINGS_5_TIER.index(rm_rating)
    fi = RATINGS_5_TIER.index(final_rating)
    if fi == ri:
        return "confirm"
    return "downgrade" if fi > ri else "upgrade"


# ---------------------------------------------------------------------------
# 표본 1개 재판정
# ---------------------------------------------------------------------------


def rejudge_sample(
    structured_llm: Any | None, plain_llm: Any, sample: dict[str, Any]
) -> dict[str, Any]:
    """표본 하나에 대해 PM을 1회 재판정하고 결과 행(dict)을 반환한다. 예외는 격리한다."""
    row: dict[str, Any] = {
        "root": sample["root"],
        "ticker": sample["ticker"],
        "date": sample["date"],
    }
    try:
        state = sample["state"]
        rm_rating = parse_rating(state["investment_plan"], context="pm_probe:rm")
        row["rm_rating"] = rm_rating

        prompt = build_portfolio_manager_prompt(state)
        row["prompt_chars"] = len(prompt)

        t0 = time.monotonic()
        decision = structured_llm.invoke(prompt) if structured_llm is not None else None
        if isinstance(decision, PortfolioDecision):
            row["final_rating"] = decision.rating.value
            # 모델이 스스로 보고한 감독 필드(질적 분석·정합성 확인용).
            row["model_override_action"] = decision.override_action
            row["model_rm_rating"] = decision.rm_proposed_rating.value
            row["override_rationale"] = decision.override_rationale
        else:
            # 구조화 실패 → 자유 텍스트 폴백 + 등급 파싱 (감독 필드는 없음).
            text = plain_llm.invoke(prompt + RATING_LINE_INSTRUCTION)
            content = text.content if hasattr(text, "content") else str(text)
            row["final_rating"] = parse_rating(content, context="pm_probe:pm-fallback")
            row["fallback"] = True
        row["elapsed_s"] = round(time.monotonic() - t0, 1)

        row["override"] = classify_override(rm_rating, row["final_rating"])
    except Exception as exc:  # noqa: BLE001 — 실패 표본은 격리하고 실험을 계속한다
        row["error"] = f"{type(exc).__name__}: {exc}"
    return row


# ---------------------------------------------------------------------------
# 실행 루프
# ---------------------------------------------------------------------------


def run_probe(
    structured_llm: Any | None,
    plain_llm: Any,
    samples: list[dict[str, Any]],
    out_jsonl: Path,
    workers: int = 5,
) -> list[dict[str, Any]]:
    """표본들을 (병렬) 재판정하고 결과를 JSONL로 즉시 기록한다."""
    rows: list[dict[str, Any]] = []
    with open(out_jsonl, "a", encoding="utf-8") as f:
        if workers > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(rejudge_sample, structured_llm, plain_llm, s): s
                    for s in samples
                }
                for i, fut in enumerate(concurrent.futures.as_completed(futures), 1):
                    row = fut.result()
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                    f.flush()
                    rows.append(row)
                    _print_progress(i, len(samples), row)
        else:
            for i, s in enumerate(samples, 1):
                row = rejudge_sample(structured_llm, plain_llm, s)
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                f.flush()
                rows.append(row)
                _print_progress(i, len(samples), row)
    return rows


def _print_progress(i: int, total: int, row: dict[str, Any]) -> None:
    if "error" in row:
        detail = f"ERROR {row['error']}"
    else:
        detail = f"RM {row['rm_rating']} → {row['final_rating']} [{row['override']}]"
    print(f"[{i}/{total}] {row['ticker']} {row['date']} ({row['root']}): {detail}", flush=True)


# ---------------------------------------------------------------------------
# 결과 요약 (마크다운)
# ---------------------------------------------------------------------------


def summarize(rows: list[dict[str, Any]]) -> str:
    """override율·방향 분포·등급 분포·질적 사례를 마크다운으로 요약한다."""
    ok = [r for r in rows if "override" in r]
    n = len(ok)
    n_err = sum(1 for r in rows if "error" in r)

    confirms = [r for r in ok if r["override"] == "confirm"]
    downgrades = [r for r in ok if r["override"] == "downgrade"]
    upgrades = [r for r in ok if r["override"] == "upgrade"]
    n_override = len(downgrades) + len(upgrades)
    override_pct = (100 * n_override / n) if n else 0.0

    def dist(key: str) -> dict[str, int]:
        return {t: sum(1 for r in ok if r.get(key) == t) for t in RATINGS_5_TIER}

    rm_dist = dist("rm_rating")
    final_dist = dist("final_rating")

    lines = [
        "# pm_probe 결과 요약 (리스크 감독 게이트 재판정 — BACKLOG.md B2)",
        "",
        f"생성 시각: {_dt.datetime.now().isoformat(timespec='seconds')}",
        "",
        f"성공 표본: {n} / 실패: {n_err}",
        "",
        "## override율 · 방향 분포",
        "",
        "| 지표 | 값 |",
        "|---|---:|",
        f"| override율 (final ≠ RM) | **{override_pct:.1f}%** ({n_override}/{n}) |",
        f"| confirm (밴드 유지) | {len(confirms)} |",
        f"| downgrade (하향, 자본 보호) | {len(downgrades)} |",
        f"| upgrade (상향) | {len(upgrades)} |",
        "",
        "목표 범위: override율 15~45% / 방향은 하향(downgrade) 우위.",
        "",
        "## 등급 분포 (RM 앵커 vs PM 최종)",
        "",
        "| 등급 | RM | PM 최종 |",
        "|---|---:|---:|",
    ]
    for t in RATINGS_5_TIER:
        lines.append(f"| {t} | {rm_dist[t]} | {final_dist[t]} |")

    # 두 로그 체제별 override율 (교정 전/후 재현성 확인).
    lines += ["", "## 로그 체제별 override율", "", "| 체제 | n | override | 하향 | 상향 |",
              "|---|---:|---:|---:|---:|"]
    roots = sorted({r["root"] for r in ok})
    for root in roots:
        sub = [r for r in ok if r["root"] == root]
        sd = sum(1 for r in sub if r["override"] == "downgrade")
        su = sum(1 for r in sub if r["override"] == "upgrade")
        lines.append(f"| {root} | {len(sub)} | {sd + su} | {sd} | {su} |")

    # 질적 사례: override(하향·상향) 표본의 근거 요약.
    lines += ["", "## override 사례 (질적)", ""]
    override_rows = downgrades + upgrades
    if not override_rows:
        lines.append("(override 없음)")
    for r in override_rows:
        rationale = r.get("override_rationale", "(폴백 — 근거 없음)")
        lines.append(
            f"- **{r['ticker']} {r['date']}** ({r['root']}): "
            f"{r['rm_rating']} → {r['final_rating']} [{r['override']}]\n"
            f"  - 근거: {rationale}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="포트폴리오 매니저 재판정 프로브 — 리스크 감독 게이트 override율 측정 (BACKLOG.md B2)",
    )
    parser.add_argument(
        "--log-roots",
        default=",".join(str(p) for p in DEFAULT_LOG_ROOTS),
        help="쉼표 구분 상태 로그 루트 목록 (기본: backtest, backtest_phase3)",
    )
    parser.add_argument(
        "--tickers", default=None,
        help="쉼표 구분 티커 필터 (기본: 모든 티커)",
    )
    parser.add_argument("--limit", type=int, default=None, help="표본 수 상한")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="LLM 호출 없이 프롬프트만 생성해 out 디렉터리에 저장",
    )
    parser.add_argument(
        "--max-calls", type=int, default=50,
        help="총 LLM 호출 수 상한 (기본: 50, 초과 시 시작 전 중단)",
    )
    parser.add_argument(
        "--workers", type=int, default=5,
        help="표본 병렬 재판정 수 (기본: 5)",
    )
    parser.add_argument("--out", default=None, help="결과 출력 디렉터리")
    args = parser.parse_args()

    # 전역 소켓 타임아웃: 타임아웃 없는 HTTP 호출로 인한 무한 대기 방지
    # (편향검증-실험-결과.md·BACKLOG.md B1 실행 노트와 동일한 방어책).
    socket.setdefaulttimeout(120)

    log_roots = [Path(p.strip()) for p in args.log_roots.split(",") if p.strip()]
    tickers = [t.strip() for t in args.tickers.split(",")] if args.tickers else None
    samples = load_samples(log_roots, tickers)
    if args.limit is not None:
        samples = samples[: args.limit]
    if not samples:
        print("오류: 상태 로그 표본이 없습니다.")
        return 1

    total_calls = len(samples)  # 표본당 1회 (구조화 실패 시 폴백 1회 추가 가능)
    print(f"표본 {len(samples)}개 (루트별: "
          + ", ".join(f"{root.name}="
                      f"{sum(1 for s in samples if s['root'] == root.name)}"
                      for root in log_roots if root.is_dir())
          + f") → 총 PM 호출 수 ~{total_calls}")
    if not args.dry_run and total_calls > args.max_calls:
        print(
            f"오류: 총 호출 수 {total_calls}가 상한 {args.max_calls}을 초과합니다. "
            "--limit 또는 --tickers로 줄이세요."
        )
        return 1

    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out) if args.out else (
        Path.home() / ".tradingagents" / "logs" / "pm_probe" / f"run_{ts}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"출력 디렉터리: {out_dir}")

    if args.dry_run:
        prompt_dir = out_dir / "prompts"
        prompt_dir.mkdir(exist_ok=True)
        for s in samples:
            path = prompt_dir / f"{s['root']}_{s['ticker']}_{s['date']}.txt"
            path.write_text(build_portfolio_manager_prompt(s["state"]), encoding="utf-8")
        print(f"dry-run 완료: {len(samples)}개 프롬프트 저장 → {prompt_dir}")
        return 0

    # LLM 클라이언트 — 파이프라인과 동일한 팩토리 + deep_think_llm 사용.
    from tradingagents.dataflows.config import get_config
    from tradingagents.llm_clients import create_llm_client

    config = get_config()
    llm_kwargs: dict[str, Any] = {}
    temperature = config.get("temperature")
    if temperature is not None and temperature != "":
        llm_kwargs["temperature"] = float(temperature)
    client = create_llm_client(
        provider=config["llm_provider"],
        model=config["deep_think_llm"],
        base_url=config.get("backend_url"),
        **llm_kwargs,
    )
    llm = client.get_llm()
    structured_llm = bind_structured(llm, PortfolioDecision, "pm_probe")
    print(f"LLM: provider={config['llm_provider']} model={config['deep_think_llm']}")

    out_jsonl = out_dir / "results.jsonl"
    rows = run_probe(structured_llm, llm, samples, out_jsonl, workers=max(1, args.workers))

    summary = summarize(rows)
    summary_path = out_dir / "summary.md"
    summary_path.write_text(summary, encoding="utf-8")
    print()
    print(summary)
    print(f"\n결과: {out_jsonl}\n요약: {summary_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
