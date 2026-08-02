#!/usr/bin/env python3
# =============================================================================
# [스크립트 개요]
# 리스크 토론 재생성 + PM 게이트 결합 검증 프로브 (편향검증 b' — 리스크 토론 대칭화).
#
# 배경: PM을 리스크 감독 게이트로 강화(BACKLOG.md B2, portfolio_manager.py)했더니
# override 22/40이 **전부 하향(0 상향)**, 문턱 2회 상향에도 55% 정체였다. 근본 원인은
# PM이 아니라 **리스크 토론 자체의 하방 편향**이었다 — 보수 분석가가 항상 구체적
# 하방 논거를 생산하고 공격 반박은 일반론으로 읽혀, 저장된 대본을 읽는 PM이 대부분
# 하향한다. 연구 토론 강세 편향의 거울상. 이를 교정하려 3인 토론자 프롬프트를
# 대칭화(risk_mgmt/{aggressive,conservative,neutral}_debator.py)했다.
#
# 이 프로브의 핵심: PM만 재판정하는 scripts/pm_probe.py로는 부족하다 — PM은
# *저장된(편향된) 리스크 토론 대본*을 읽기 때문이다. 교정 효과를 보려면 리스크
# 토론을 **교정된 프롬프트로 재생성**한 뒤 PM 게이트를 적용해야 한다. 그래서 이
# 프로브는 저장된 상태 로그 40개(backtest 27 + backtest_phase3 13)를 재활용해,
# 각 로그의 분석가 4종 보고서·investment_plan·trader_plan을 입력으로 리스크 토론
# 3인을 **실제 노드 함수 + 실제 conditional_logic(3N+1)**로 새로 돌리고, 그 위에
# PM 게이트를 실행해 최종 등급 분포와 override 방향을 집계한다.
#
# 목표(직전 "전부 하향 55%" 대비):
#   - override 방향이 **양방향**(하향만이 아니라 상향도 등장)
#   - RM 앵커 분포 대비 최종 분포가 한쪽으로 쏠리지 않음(전부 Hold 하향 해소)
#   - override율은 참고치 15~45%지만, 더 중요한 건 방향 균형과 약세 일괄 이동 방지
#
# 실행 예:
#   TRADINGAGENTS_LLM_PROVIDER=bedrock \
#   TRADINGAGENTS_DEEP_THINK_LLM=us.anthropic.claude-sonnet-4-5-20250929-v1:0 \
#   TRADINGAGENTS_QUICK_THINK_LLM=us.anthropic.claude-haiku-4-5-20251001-v1:0 \
#   AWS_DEFAULT_REGION=us-east-1 \
#   .venv/bin/python scripts/risk_debate_probe.py --limit 9          # 파일럿
#   .venv/bin/python scripts/risk_debate_probe.py                    # 전체 40
#   .venv/bin/python scripts/risk_debate_probe.py --dry-run --limit 2
# =============================================================================
"""리스크 토론 재생성 + PM 게이트 결합 프로브: 리스크 토론 대칭화의 결합 효과 측정."""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import signal
import socket
import sys
import threading
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
from tradingagents.agents.risk_mgmt.aggressive_debator import (  # noqa: E402
    create_aggressive_debator,
)
from tradingagents.agents.risk_mgmt.conservative_debator import (  # noqa: E402
    create_conservative_debator,
)
from tradingagents.agents.risk_mgmt.neutral_debator import (  # noqa: E402
    create_neutral_debator,
)
from tradingagents.agents.schemas import PortfolioDecision  # noqa: E402
from tradingagents.agents.utils.rating import RATINGS_5_TIER, parse_rating  # noqa: E402
from tradingagents.agents.utils.structured import (  # noqa: E402
    RATING_LINE_INSTRUCTION,
    bind_structured,
)
from tradingagents.graph.conditional_logic import ConditionalLogic  # noqa: E402

# 기본 상태 로그 루트 2곳 (편향검증 Phase 3 이전 27 + Phase 3 교정 후 13 = 40).
DEFAULT_LOG_ROOTS = (
    Path.home() / ".tradingagents" / "logs" / "backtest",
    Path.home() / ".tradingagents" / "logs" / "backtest_phase3",
)

BULLISH = {"Buy", "Overweight"}
BEARISH = {"Underweight", "Sell"}

# 리스크 토론 첫 발언자: 라이브 그래프의 Trader → Aggressive Analyst 엣지와 동일.
FIRST_SPEAKER = "Aggressive Analyst"


class ComboTimeoutError(Exception):
    """표본 하나(리스크 토론 3~4턴 + PM)가 시간 상한을 초과했음을 알린다 (watchdog)."""


# ---------------------------------------------------------------------------
# 표본 로드 · 상태 정규화 (pm_probe와 동일 계약)
# ---------------------------------------------------------------------------


def load_samples(
    log_roots: list[Path], tickers: list[str] | None = None
) -> list[dict[str, Any]]:
    """여러 백테스트 상태 로그 루트를 모두 로드해 표본 리스트로 만든다."""
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
    """저장된 상태 로그를 라이브 노드가 기대하는 키로 정규화한다.

    - 저장 로그는 트레이더 산출물을 ``trader_investment_decision``으로
      직렬화하지만 라이브 노드는 ``trader_investment_plan``을 읽는다.
    - 리스크 토론자는 4종 분석 보고서를 읽으므로, 일부 분석가 미선택
      실행에서 키가 없으면 빈 문자열로 채운다.
    - **핵심**: 저장 로그의 ``risk_debate_state``(편향된 옛 대본)를 버리고
      빈 상태로 초기화해, 교정된 프롬프트로 토론을 처음부터 재생성한다.
    """
    state = dict(state)
    if "trader_investment_plan" not in state:
        state["trader_investment_plan"] = state.get("trader_investment_decision", "")
    for k in ("market_report", "sentiment_report", "news_report", "fundamentals_report"):
        state.setdefault(k, "")
    state["risk_debate_state"] = fresh_risk_debate_state()
    return state


def fresh_risk_debate_state() -> dict[str, Any]:
    """라이브 그래프(propagation.py)와 동일한 빈 리스크 토론 초기 상태."""
    return {
        "aggressive_history": "",
        "conservative_history": "",
        "neutral_history": "",
        "history": "",
        "latest_speaker": "",
        "current_aggressive_response": "",
        "current_conservative_response": "",
        "current_neutral_response": "",
        "judge_decision": "",
        "count": 0,
    }


# ---------------------------------------------------------------------------
# 인터리브 (표본별 — 실패·정지가 한 티커에 몰리지 않게 라운드로빈)
# ---------------------------------------------------------------------------


def interleave(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """(root, ticker) 그룹별로 라운드로빈 인터리브한다 (원소 보존)."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for s in samples:
        groups.setdefault((s["root"], s["ticker"]), []).append(s)
    ordered: list[dict[str, Any]] = []
    queues = list(groups.values())
    i = 0
    while any(queues):
        q = queues[i % len(queues)]
        if q:
            ordered.append(q.pop(0))
        i += 1
        if i % len(queues) == 0:
            queues = [q for q in queues if q]
            i = 0
            if not queues:
                break
    return ordered


# ---------------------------------------------------------------------------
# override 분류 (밴드 이동 방향) — pm_probe와 동일
# ---------------------------------------------------------------------------


def classify_override(rm_rating: str, final_rating: str) -> str:
    """RM 등급 대비 최종 등급 밴드 이동을 confirm/upgrade/downgrade로 분류한다.

    RATINGS_5_TIER는 강세(Buy)→약세(Sell) 순이라 인덱스가 커질수록 약세.
    최종 인덱스 > RM → 하향(downgrade), < RM → 상향(upgrade), == → 확정(confirm).
    """
    ri = RATINGS_5_TIER.index(rm_rating)
    fi = RATINGS_5_TIER.index(final_rating)
    if fi == ri:
        return "confirm"
    return "downgrade" if fi > ri else "upgrade"


# ---------------------------------------------------------------------------
# 리스크 토론 재생성 (실제 노드 + 실제 conditional_logic, 3N+1)
# ---------------------------------------------------------------------------


def run_risk_debate(
    state: dict[str, Any],
    nodes: dict[str, Any],
    cond_logic: ConditionalLogic,
) -> tuple[dict[str, Any], int]:
    """리스크 토론을 라이브 라우팅 그대로 재생성한다.

    Trader → Aggressive Analyst 엣지로 공격 분석가가 개시하고,
    should_continue_risk_analysis(3N+1)가 다음 발언자/종료를 결정한다.
    각 노드는 {"risk_debate_state": ...} 갱신을 반환하므로 상태에 병합한다.
    """
    state = {**state, "risk_debate_state": fresh_risk_debate_state()}
    current = FIRST_SPEAKER
    turns = 0
    # 안전 상한: 3N+1 이론 최대의 2배 (라우팅 버그로 인한 무한 루프 방어).
    hard_cap = 2 * (3 * cond_logic.max_risk_discuss_rounds + 1) + 2
    while True:
        update = nodes[current](state)
        state = {**state, **update}
        turns += 1
        nxt = cond_logic.should_continue_risk_analysis(state)
        if nxt == "Portfolio Manager" or turns >= hard_cap:
            break
        current = nxt
    return state, turns


# ---------------------------------------------------------------------------
# 표본 1개 처리 (리스크 토론 재생성 → PM 게이트) + 조합당 watchdog
# ---------------------------------------------------------------------------


def process_sample(
    sample: dict[str, Any],
    nodes: dict[str, Any],
    cond_logic: ConditionalLogic,
    structured_pm_llm: Any | None,
    plain_pm_llm: Any,
    combo_timeout: int | None,
) -> dict[str, Any]:
    """표본 하나를 재생성+재판정하고 결과 행을 반환한다. 예외·타임아웃은 격리한다."""
    row: dict[str, Any] = {
        "root": sample["root"],
        "ticker": sample["ticker"],
        "date": sample["date"],
    }

    use_alarm = bool(
        combo_timeout
        and hasattr(signal, "SIGALRM")
        and threading.current_thread() is threading.main_thread()
    )

    def _on_alarm(signum, frame):
        raise ComboTimeoutError(f"sample exceeded {combo_timeout}s (watchdog)")

    old_handler = None
    try:
        if use_alarm:
            old_handler = signal.signal(signal.SIGALRM, _on_alarm)
            signal.alarm(combo_timeout)

        state = sample["state"]
        rm_rating = parse_rating(state["investment_plan"], context="risk_debate_probe:rm")
        row["rm_rating"] = rm_rating

        t0 = time.monotonic()
        # 1) 리스크 토론을 교정된 프롬프트로 재생성.
        state, n_turns = run_risk_debate(state, nodes, cond_logic)
        row["debate_turns"] = n_turns

        # 2) 재생성된 토론 위에 PM 게이트 실행.
        prompt = build_portfolio_manager_prompt(state)
        row["prompt_chars"] = len(prompt)
        decision = (
            structured_pm_llm.invoke(prompt) if structured_pm_llm is not None else None
        )
        if isinstance(decision, PortfolioDecision):
            row["final_rating"] = decision.rating.value
            row["model_override_action"] = decision.override_action
            row["model_rm_rating"] = decision.rm_proposed_rating.value
            row["override_rationale"] = decision.override_rationale
        else:
            text = plain_pm_llm.invoke(prompt + RATING_LINE_INSTRUCTION)
            content = text.content if hasattr(text, "content") else str(text)
            row["final_rating"] = parse_rating(content, context="risk_debate_probe:pm-fallback")
            row["fallback"] = True
        row["elapsed_s"] = round(time.monotonic() - t0, 1)
        row["override"] = classify_override(rm_rating, row["final_rating"])
    except Exception as exc:  # noqa: BLE001 — 실패 표본은 격리하고 실험을 계속한다
        row["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if use_alarm:
            signal.alarm(0)
            if old_handler is not None:
                signal.signal(signal.SIGALRM, old_handler)
    return row


# ---------------------------------------------------------------------------
# 실행 루프 (단일 스레드 — SIGALRM watchdog은 메인 스레드에서만 동작)
# ---------------------------------------------------------------------------


def run_probe(
    samples: list[dict[str, Any]],
    nodes: dict[str, Any],
    cond_logic: ConditionalLogic,
    structured_pm_llm: Any | None,
    plain_pm_llm: Any,
    out_jsonl: Path,
    combo_timeout: int | None,
) -> list[dict[str, Any]]:
    """표본들을 순차 처리하고 결과를 JSONL로 즉시 기록한다 (실패 격리)."""
    rows: list[dict[str, Any]] = []
    total = len(samples)
    with open(out_jsonl, "a", encoding="utf-8") as f:
        for i, s in enumerate(samples, 1):
            row = process_sample(
                s, nodes, cond_logic, structured_pm_llm, plain_pm_llm, combo_timeout
            )
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            rows.append(row)
            _print_progress(i, total, row)
    return rows


def _print_progress(i: int, total: int, row: dict[str, Any]) -> None:
    if "error" in row:
        detail = f"ERROR {row['error']}"
    else:
        detail = (
            f"RM {row['rm_rating']} → {row['final_rating']} "
            f"[{row['override']}] ({row.get('debate_turns', '?')}턴, {row.get('elapsed_s', '?')}s)"
        )
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

    def band_pct(d: dict[str, int]) -> tuple[float, float, float]:
        tot = sum(d.values()) or 1
        bull = sum(d[t] for t in BULLISH)
        bear = sum(d[t] for t in BEARISH)
        hold = d["Hold"]
        return 100 * bull / tot, 100 * hold / tot, 100 * bear / tot

    rm_b = band_pct(rm_dist)
    fin_b = band_pct(final_dist)

    lines = [
        "# risk_debate_probe 결과 요약 (리스크 토론 재생성 + PM 게이트 — 편향검증 b')",
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
        f"| downgrade (하향) | {len(downgrades)} |",
        f"| upgrade (상향) | {len(upgrades)} |",
        "",
        "목표: override 방향이 **양방향**(상향도 등장), 최종 분포가 약세로 일괄 이동하지 않음.",
        "참고: 직전 pm_probe(저장 대본, 편향 미교정)는 22/40 override가 **전부 하향, 0 상향**.",
        "",
        "## 등급 분포 (RM 앵커 vs PM 최종)",
        "",
        "| 등급 | RM | PM 최종 |",
        "|---|---:|---:|",
    ]
    for t in RATINGS_5_TIER:
        lines.append(f"| {t} | {rm_dist[t]} | {final_dist[t]} |")
    lines += [
        "",
        "| 밴드 | RM | PM 최종 |",
        "|---|---:|---:|",
        f"| 강세(Buy+Overweight) | {rm_b[0]:.1f}% | {fin_b[0]:.1f}% |",
        f"| Hold | {rm_b[1]:.1f}% | {fin_b[1]:.1f}% |",
        f"| 약세(Underweight+Sell) | {rm_b[2]:.1f}% | {fin_b[2]:.1f}% |",
    ]

    # 두 로그 체제별 override율 (교정 전/후 로그 재현성 확인).
    lines += ["", "## 로그 체제별 override율", "", "| 체제 | n | override | 하향 | 상향 |",
              "|---|---:|---:|---:|---:|"]
    for root in sorted({r["root"] for r in ok}):
        sub = [r for r in ok if r["root"] == root]
        sd = sum(1 for r in sub if r["override"] == "downgrade")
        su = sum(1 for r in sub if r["override"] == "upgrade")
        lines.append(f"| {root} | {len(sub)} | {sd + su} | {sd} | {su} |")

    # 질적 사례: override(하향·상향) 표본의 근거 요약.
    lines += ["", "## override 사례 (질적)", ""]
    override_rows = upgrades + downgrades  # 상향을 먼저 노출 (양방향 신호가 핵심)
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
        description="리스크 토론 재생성 + PM 게이트 결합 프로브 (편향검증 b' — 리스크 토론 대칭화)",
    )
    parser.add_argument(
        "--log-roots",
        default=",".join(str(p) for p in DEFAULT_LOG_ROOTS),
        help="쉼표 구분 상태 로그 루트 목록 (기본: backtest, backtest_phase3)",
    )
    parser.add_argument("--tickers", default=None, help="쉼표 구분 티커 필터 (기본: 전체)")
    parser.add_argument("--limit", type=int, default=None, help="표본 수 상한 (파일럿: --limit 9)")
    parser.add_argument("--rounds", type=int, default=1, help="리스크 토론 라운드 N (발언 3N+1, 기본 1)")
    parser.add_argument("--dry-run", action="store_true", help="LLM 호출 없이 표본·호출 계획만 출력")
    parser.add_argument(
        "--max-calls", type=int, default=300,
        help="총 LLM 호출 수 상한 (기본 300; 초과 시 시작 전 중단)",
    )
    parser.add_argument(
        "--combo-timeout", type=int, default=600,
        help="표본 하나(토론+PM)의 시간 상한 초 (SIGALRM watchdog, 기본 600)",
    )
    parser.add_argument("--out", default=None, help="결과 출력 디렉터리")
    args = parser.parse_args()

    # 전역 소켓 타임아웃: 타임아웃 없는 HTTP 호출로 인한 무한 대기 방지
    # (편향검증-실험-결과.md·BACKLOG.md B1 실행 노트와 동일한 방어책).
    socket.setdefaulttimeout(120)

    log_roots = [Path(p.strip()) for p in args.log_roots.split(",") if p.strip()]
    tickers = [t.strip() for t in args.tickers.split(",")] if args.tickers else None
    samples = load_samples(log_roots, tickers)
    samples = interleave(samples)  # 표본별 인터리브
    if args.limit is not None:
        samples = samples[: args.limit]
    if not samples:
        print("오류: 상태 로그 표본이 없습니다.")
        return 1

    calls_per_sample = (3 * args.rounds + 1) + 1  # 토론 3N+1턴 + PM 1회
    total_calls = len(samples) * calls_per_sample
    root_counts = ", ".join(
        f"{root.name}={sum(1 for s in samples if s['root'] == root.name)}"
        for root in log_roots if root.is_dir()
    )
    print(
        f"표본 {len(samples)}개 ({root_counts}) × 표본당 {calls_per_sample}호출 "
        f"(리스크 토론 {3 * args.rounds + 1}턴 + PM 1) → 총 LLM 호출 수 ~{total_calls}"
    )
    if not args.dry_run and total_calls > args.max_calls:
        print(
            f"오류: 총 호출 수 {total_calls}가 상한 {args.max_calls}을 초과합니다. "
            "--limit / --tickers 로 줄이거나 --max-calls 를 올리세요."
        )
        return 1

    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out) if args.out else (
        Path.home() / ".tradingagents" / "logs" / "risk_debate_probe" / f"run_{ts}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"출력 디렉터리: {out_dir}")

    if args.dry_run:
        manifest = out_dir / "dry_run_plan.txt"
        with open(manifest, "w", encoding="utf-8") as f:
            f.write(f"표본 {len(samples)}개 × {calls_per_sample}호출 = ~{total_calls} 호출\n\n")
            for i, s in enumerate(samples, 1):
                rm = parse_rating(s["state"]["investment_plan"], context="dry-run")
                f.write(f"[{i}] {s['ticker']} {s['date']} ({s['root']}) — RM 앵커: {rm}\n")
        print(f"dry-run 완료: 실행 계획 저장 → {manifest}")
        return 0

    # LLM 클라이언트 — 라이브 그래프와 동일: 토론자=quick, PM=deep.
    from tradingagents.dataflows.config import get_config
    from tradingagents.llm_clients import create_llm_client

    config = get_config()
    llm_kwargs: dict[str, Any] = {}
    temperature = config.get("temperature")
    if temperature is not None and temperature != "":
        llm_kwargs["temperature"] = float(temperature)

    def _make(model: str) -> Any:
        return create_llm_client(
            provider=config["llm_provider"],
            model=model,
            base_url=config.get("backend_url"),
            **llm_kwargs,
        ).get_llm()

    quick_llm = _make(config["quick_think_llm"])
    deep_llm = _make(config["deep_think_llm"])
    structured_pm_llm = bind_structured(deep_llm, PortfolioDecision, "risk_debate_probe")
    print(
        f"LLM: provider={config['llm_provider']} "
        f"debators(quick)={config['quick_think_llm']} PM(deep)={config['deep_think_llm']}"
    )

    # 실제 노드 함수 + 실제 conditional_logic 재사용 (프로브가 운영 로직과 어긋나지 않게).
    nodes = {
        "Aggressive Analyst": create_aggressive_debator(quick_llm),
        "Conservative Analyst": create_conservative_debator(quick_llm),
        "Neutral Analyst": create_neutral_debator(quick_llm),
    }
    cond_logic = ConditionalLogic(max_risk_discuss_rounds=args.rounds)

    out_jsonl = out_dir / "results.jsonl"
    rows = run_probe(
        samples, nodes, cond_logic, structured_pm_llm, deep_llm, out_jsonl,
        combo_timeout=args.combo_timeout,
    )

    summary = summarize(rows)
    summary_path = out_dir / "summary.md"
    summary_path.write_text(summary, encoding="utf-8")
    print()
    print(summary)
    print(f"\n결과: {out_jsonl}\n요약: {summary_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
