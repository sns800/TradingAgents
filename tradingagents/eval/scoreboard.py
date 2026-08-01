# =============================================================================
# [모듈 개요 — 초보자용]
# 이 파일은 메모리 로그(TradingMemoryLog)에 쌓인 "해소된(resolved)" 결정들을
# 집계해 스코어보드를 만드는 순수 로직입니다. LLM·네트워크 호출이 전혀 없는
# 결정론적(deterministic) 계산이라 단위 테스트가 쉽습니다.
#
# 핵심 개념 — 방향 적중(directional hit):
#   - Buy / Overweight  : 알파(벤치마크 대비 초과 수익) > 0 이면 적중
#   - Sell / Underweight: 알파 < 0 이면 적중
#   - Hold              : |알파| < 임계값(hold_threshold) 이면 적중
#     ("시장과 비슷하게 갈 것"이라는 예측이므로, 알파가 임계값 안에
#      머물렀는지가 적중 기준입니다)
#
# 베이스라인 비교 — 같은 표본에서의 기대값:
#   - 항상-Hold: 모든 결정이 Hold였다면 얻었을 적중률
#   - 랜덤:      5개 등급을 균등 확률로 찍었을 때의 기대 적중률
#   파이프라인의 적중률이 이 둘을 넘지 못하면 "토론이 품질을 높인다"는
#   설계 가정이 데이터로 뒷받침되지 않는다는 신호입니다.
# =============================================================================

"""메모리 로그 결정 집계 스코어보드 — 등급별 방향 적중률·평균 알파·베이스라인 비교."""

from __future__ import annotations

from tradingagents.agents.utils.rating import RATINGS_5_TIER

# 강세(bullish) / 약세(bearish) 등급 집합 — 방향 적중 판정에 사용
BULLISH_RATINGS = frozenset({"Buy", "Overweight"})
BEARISH_RATINGS = frozenset({"Sell", "Underweight"})

# Hold 적중 판정용 |알파| 임계값 기본치 (0.01 = 1%)
DEFAULT_HOLD_THRESHOLD = 0.01


def parse_percent(value) -> float | None:
    """메모리 로그의 수익률 표기("+1.2%")를 소수(0.012)로 변환한다.

    숫자(float/int)가 들어오면 이미 소수 비율로 간주해 그대로 반환합니다.
    None, 빈 문자열, "n/a", "pending" 등 파싱 불가능한 값은 None을 반환합니다.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip()
    if not text or text.lower() in ("n/a", "none", "pending"):
        return None
    is_percent = text.endswith("%")
    if is_percent:
        text = text[:-1].strip()
    try:
        number = float(text)
    except ValueError:
        return None
    return number / 100.0 if is_percent else number


def is_directional_hit(
    rating: str, alpha: float, hold_threshold: float = DEFAULT_HOLD_THRESHOLD
) -> bool:
    """등급의 방향 예측이 실현 알파와 부합했는지 판정한다.

    Buy/Overweight는 알파 > 0, Sell/Underweight는 알파 < 0,
    Hold는 |알파| < hold_threshold이면 적중입니다.
    """
    if rating in BULLISH_RATINGS:
        return alpha > 0
    if rating in BEARISH_RATINGS:
        return alpha < 0
    # Hold(및 미지의 등급은 보수적으로 Hold 규칙 적용)
    return abs(alpha) < hold_threshold


def _expected_random_hit(alpha: float, hold_threshold: float) -> float:
    """등급을 5개 중 균등 확률로 찍었을 때, 이 표본에서의 적중 확률.

    Buy/Overweight 2개는 알파>0일 때, Sell/Underweight 2개는 알파<0일 때,
    Hold 1개는 |알파|<임계값일 때 적중하므로 각각의 지시함수(indicator)에
    1/5 가중을 곱해 더합니다.
    """
    hits = 0
    hits += 2 * (1 if alpha > 0 else 0)
    hits += 2 * (1 if alpha < 0 else 0)
    hits += 1 if abs(alpha) < hold_threshold else 0
    return hits / len(RATINGS_5_TIER)


def _mean(values: list[float]) -> float | None:
    # 빈 리스트면 None(표본 없음)을 반환하는 안전한 평균
    if not values:
        return None
    return sum(values) / len(values)


def aggregate_entries(
    entries: list[dict], hold_threshold: float = DEFAULT_HOLD_THRESHOLD
) -> dict:
    """메모리 로그 항목 리스트를 집계해 스코어보드 요약 dict를 만든다.

    입력은 ``TradingMemoryLog.load_entries()``가 반환하는 dict 형태를
    기대합니다 (키: rating, raw, alpha, pending). raw/alpha는 "+1.2%" 형태의
    문자열 또는 소수 비율 float 모두 허용합니다. pending 항목과 알파를
    파싱할 수 없는 항목은 건너뛰고 ``skipped``에 집계합니다.

    반환 구조:
        {
          "hold_threshold": float,
          "total": int,          # 판정에 포함된 항목 수
          "skipped": int,        # pending 또는 파싱 불가로 제외된 항목 수
          "per_rating": {rating: {count, hits, hit_rate, avg_raw, avg_alpha}},
          "overall": {count, hits, hit_rate, avg_raw, avg_alpha},
          "baselines": {"always_hold_hit_rate": float|None,
                        "random_hit_rate": float|None},
        }
    """
    per_rating: dict[str, dict] = {
        r: {"count": 0, "hits": 0, "raws": [], "alphas": []} for r in RATINGS_5_TIER
    }
    skipped = 0
    total_hits = 0
    all_raws: list[float] = []
    all_alphas: list[float] = []
    hold_baseline_hits = 0.0
    random_baseline_hits = 0.0

    for entry in entries:
        # pending(결과 대기)과 unresolved(가격 데이터가 영구히 없어 해소를
        # 포기한 항목)는 실현 수익률이 없으므로 판정에서 제외한다.
        # unresolved는 알파 필드도 없어 아래 parse_percent에서도 걸러지지만,
        # 여기서 명시적으로 제외해 의도를 분명히 한다.
        if entry.get("pending") or entry.get("unresolved"):
            skipped += 1
            continue
        alpha = parse_percent(entry.get("alpha"))
        if alpha is None:
            skipped += 1
            continue
        rating = entry.get("rating") or "Hold"
        if rating not in per_rating:
            # 어휘 밖 등급은 표를 오염시키지 않도록 별도 버킷으로 수집
            per_rating[rating] = {"count": 0, "hits": 0, "raws": [], "alphas": []}
        raw = parse_percent(entry.get("raw"))

        hit = is_directional_hit(rating, alpha, hold_threshold)
        bucket = per_rating[rating]
        bucket["count"] += 1
        bucket["hits"] += 1 if hit else 0
        bucket["alphas"].append(alpha)
        if raw is not None:
            bucket["raws"].append(raw)

        total_hits += 1 if hit else 0
        all_alphas.append(alpha)
        if raw is not None:
            all_raws.append(raw)

        # 같은 표본에 대한 베이스라인 기대값 누적
        hold_baseline_hits += 1 if abs(alpha) < hold_threshold else 0
        random_baseline_hits += _expected_random_hit(alpha, hold_threshold)

    total = len(all_alphas)

    def _finalize(bucket: dict) -> dict:
        count = bucket["count"]
        return {
            "count": count,
            "hits": bucket["hits"],
            "hit_rate": (bucket["hits"] / count) if count else None,
            "avg_raw": _mean(bucket["raws"]),
            "avg_alpha": _mean(bucket["alphas"]),
        }

    return {
        "hold_threshold": hold_threshold,
        "total": total,
        "skipped": skipped,
        "per_rating": {r: _finalize(b) for r, b in per_rating.items()},
        "overall": {
            "count": total,
            "hits": total_hits,
            "hit_rate": (total_hits / total) if total else None,
            "avg_raw": _mean(all_raws),
            "avg_alpha": _mean(all_alphas),
        },
        "baselines": {
            "always_hold_hit_rate": (hold_baseline_hits / total) if total else None,
            "random_hit_rate": (random_baseline_hits / total) if total else None,
        },
    }


def _fmt_pct(value: float | None) -> str:
    # 비율(0.012)을 "+1.2%" 형태로, None은 "n/a"로 표기
    if value is None:
        return "n/a"
    return f"{value:+.2%}"


def _fmt_rate(value: float | None) -> str:
    # 적중률(0.65)을 "65.0%" 형태로, None은 "n/a"로 표기
    if value is None:
        return "n/a"
    return f"{value:.1%}"


def render_markdown(summary: dict) -> str:
    """``aggregate_entries``의 요약 dict를 마크다운 표 문자열로 렌더링한다."""
    threshold = summary["hold_threshold"]
    lines = [
        "# 메모리 로그 스코어보드",
        "",
        f"- 판정 포함 항목: {summary['total']}건 "
        f"(제외: {summary['skipped']}건 — pending/unresolved 또는 수익률 파싱 불가)",
        f"- Hold 적중 임계값: |알파| < {threshold:.2%}",
        "",
        "## 등급별 성과",
        "",
        "| 등급 | 건수 | 방향 적중률 | 평균 원수익률 | 평균 알파 |",
        "|---|---:|---:|---:|---:|",
    ]
    ordered = list(RATINGS_5_TIER) + [
        r for r in summary["per_rating"] if r not in RATINGS_5_TIER
    ]
    for rating in ordered:
        stats = summary["per_rating"][rating]
        lines.append(
            f"| {rating} | {stats['count']} | {_fmt_rate(stats['hit_rate'])} "
            f"| {_fmt_pct(stats['avg_raw'])} | {_fmt_pct(stats['avg_alpha'])} |"
        )
    overall = summary["overall"]
    lines.append(
        f"| **전체** | {overall['count']} | {_fmt_rate(overall['hit_rate'])} "
        f"| {_fmt_pct(overall['avg_raw'])} | {_fmt_pct(overall['avg_alpha'])} |"
    )

    baselines = summary["baselines"]
    lines += [
        "",
        "## 베이스라인 비교 (같은 표본에서의 기대 적중률)",
        "",
        "| 전략 | 적중률 |",
        "|---|---:|",
        f"| 파이프라인 (실제) | {_fmt_rate(overall['hit_rate'])} |",
        f"| 항상-Hold | {_fmt_rate(baselines['always_hold_hit_rate'])} |",
        f"| 랜덤 (5등급 균등) | {_fmt_rate(baselines['random_hit_rate'])} |",
    ]
    return "\n".join(lines)
