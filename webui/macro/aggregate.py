# ============================================================
# [모듈 개요] G20 매크로 집계·파생 계산 — CONTRACT.md 6장의 구현
#
# 전부 순수 함수다. 네트워크·AWS·파일 I/O가 없고, 입력은 `list[Observation]`
# (같은 indicator·iso·freq) 또는 `list[Doc]`이며 출력도 같은 타입이다.
# 수집기(sources/*)가 원천 빈도로 받은 관측치를 store.py가 저장하기 전에 이 모듈로
# 저빈도 집계(D→M/Q/Y)·전년비·지수화·의존도·여론조사 평균을 만든다.
#
# 규칙 요약
#  - agg=last  기말값: D→M(월 마지막 관측), M→Q(분기 마지막 월), Q→Y(연 마지막 분기)
#  - agg=mean  기간 평균 / agg=sum 기간 합계
#  - 달력상 아직 끝나지 않은 마지막 버킷에는 flags에 `partial_period`를 붙인다
#  - yoy는 같은 빈도의 12개월/4분기/1년 전 값과 비교한다 (월별 YoY 평균 금지)
#  - 통화가치 지수 = 기준환율 / 환율 × 100 (환율 = 현지통화/USD)
#  - 파생 산출물의 flags에는 `derived`를 붙이고 method에 계산식을 한국어로 남긴다
# ============================================================
from __future__ import annotations

import calendar
from collections import OrderedDict
from datetime import date, timedelta
from typing import Any

from macro.schema import Doc, Observation, validate_period

# 빈도 순위: 숫자가 크면 더 낮은(거친) 빈도
FREQ_RANK: dict[str, int] = {"D": 0, "E": 0, "W": 1, "M": 2, "Q": 3, "Y": 4}
# yoy 기본 시차 (D는 전년비 정의 불가)
DEFAULT_YOY_LAG: dict[str, int] = {"W": 52, "M": 12, "Q": 4, "Y": 1}

_FREQ_LABEL = {"D": "일", "W": "주", "M": "월", "Q": "분기", "Y": "연", "E": "이벤트"}
_TARGET_LABEL = {"M": "월", "Q": "분기", "Y": "연"}
_COUNT_UNIT = {"D": "일", "W": "주", "M": "개월", "Q": "분기", "E": "건"}
_DATE_LIKE = ("D", "W", "E")


# ------------------------------------------------------------------
# 기간(period) 유틸
# ------------------------------------------------------------------
def period_sort_key(freq: str, period: str) -> tuple[int, int, int]:
    """같은 freq 안에서 시간순 정렬에 쓰는 키. (연, 월/분기/일자, 일)"""
    validate_period(freq, period)
    if freq in _DATE_LIKE:
        y, m, d = period.split("-")
        return (int(y), int(m), int(d))
    if freq == "M":
        y, m = period.split("-")
        return (int(y), int(m), 0)
    if freq == "Q":
        y, q = period.split("-Q")
        return (int(y), int(q), 0)
    return (int(period), 0, 0)


def month_to_quarter(month: str) -> str:
    """`2026-08` -> `2026-Q3`"""
    validate_period("M", month)
    y, m = month.split("-")
    return f"{y}-Q{(int(m) - 1) // 3 + 1}"


def period_to_year(period: str) -> str:
    """어떤 빈도의 period든 연도 문자열(`YYYY`)로 바꾼다."""
    year = str(period)[:4]
    validate_period("Y", year)
    return year


def previous_period(freq: str, period: str, n: int = 1) -> str:
    """n기간 전 period. n이 음수면 이후 기간."""
    validate_period(freq, period)
    if freq in _DATE_LIKE:
        step = 7 if freq == "W" else 1
        d = date.fromisoformat(period) - timedelta(days=step * n)
        return d.isoformat()
    if freq == "M":
        y, m = (int(x) for x in period.split("-"))
        total = y * 12 + (m - 1) - n
        return f"{total // 12:04d}-{total % 12 + 1:02d}"
    if freq == "Q":
        y, q = (int(x) for x in period.split("-Q"))
        total = y * 4 + (q - 1) - n
        return f"{total // 4:04d}-Q{total % 4 + 1}"
    return f"{int(period) - n:04d}"


def periods_between(freq: str, from_period: str, to_period: str) -> list[str]:
    """from_period ~ to_period(양끝 포함)의 모든 period를 시간순으로."""
    validate_period(freq, from_period)
    validate_period(freq, to_period)
    if period_sort_key(freq, from_period) > period_sort_key(freq, to_period):
        return []
    out = [from_period]
    guard = 0
    while out[-1] != to_period:
        out.append(previous_period(freq, out[-1], -1))
        guard += 1
        if guard > 200_000:  # 방어: 30년 일별도 1.1만 건
            raise ValueError(f"기간이 너무 길다: {from_period}~{to_period} ({freq})")
    return out


def period_end_date(freq: str, period: str) -> date:
    """period의 달력상 마지막 날짜 (완결 여부 판단용)."""
    validate_period(freq, period)
    if freq in _DATE_LIKE:
        return date.fromisoformat(period)
    if freq == "M":
        y, m = (int(x) for x in period.split("-"))
        return date(y, m, calendar.monthrange(y, m)[1])
    if freq == "Q":
        y, q = (int(x) for x in period.split("-Q"))
        m = q * 3
        return date(y, m, calendar.monthrange(y, m)[1])
    return date(int(period), 12, 31)


def bucket_of(freq: str, period: str, target_freq: str) -> str:
    """원천 period가 속하는 저빈도 버킷 period."""
    if FREQ_RANK[target_freq] <= FREQ_RANK[freq]:
        raise ValueError(f"{freq} -> {target_freq} 집계는 지원하지 않는다 (더 낮은 빈도만 가능)")
    if target_freq == "Y":
        return period_to_year(period)
    if target_freq == "Q":
        month = period[:7] if freq in _DATE_LIKE else period
        return month_to_quarter(month)
    if target_freq == "M":
        return period[:7]
    raise ValueError(f"지원하지 않는 목표 빈도: {target_freq}")


# ------------------------------------------------------------------
# 저빈도 집계
# ------------------------------------------------------------------
def to_lower_freq(
    obs: list[Observation],
    target_freq: str,
    agg: str = "last",
    today: date | str | None = None,
) -> list[Observation]:
    """같은 지표·국가·빈도의 관측치를 더 낮은 빈도로 집계한다 (CONTRACT 6장).

    - `agg`: last(기말) | mean(평균) | sum(합계)
    - 달력상 아직 끝나지 않은 버킷(예: 8월까지 받은 2026-Q3, 2026년)에는
      `partial_period` 플래그와 method 표기를 붙인다.
    - `today`를 주면 그 날짜 기준으로 완결 여부를 판단한다(테스트 결정성).
    - unit/source/series_id/source_url/vintage와 flags는 버킷의 **마지막(대표)** 관측치를
      승계한다(폴백 소스 표기 등이 최신 상태를 따라가도록).
    """
    if agg not in ("last", "mean", "sum"):
        raise ValueError(f"지원하지 않는 집계 방식: {agg!r} (last|mean|sum)")
    if not obs:
        return []
    ref = _require_homogeneous(obs)
    if any(o.value is None for o in obs):
        raise ValueError("복합값(payload) 관측치는 저빈도 집계를 지원하지 않는다")
    src_freq = ref.freq
    if target_freq not in FREQ_RANK:
        raise ValueError(f"알 수 없는 빈도: {target_freq!r}")
    if FREQ_RANK[target_freq] <= FREQ_RANK[src_freq]:
        raise ValueError(f"{src_freq} -> {target_freq} 집계는 지원하지 않는다 (더 낮은 빈도만 가능)")

    asof = _as_date(today)
    ordered = sorted(obs, key=lambda o: period_sort_key(o.freq, o.period))
    buckets: OrderedDict[str, list[Observation]] = OrderedDict()
    for o in ordered:
        buckets.setdefault(bucket_of(src_freq, o.period, target_freq), []).append(o)

    out: list[Observation] = []
    for bucket, members in buckets.items():
        rep = members[-1]
        values = [float(o.value) for o in members]  # type: ignore[arg-type]
        if agg == "last":
            value = values[-1]
        elif agg == "mean":
            value = sum(values) / len(values)
        else:
            value = sum(values)
        partial = period_end_date(target_freq, bucket) > asof
        method = _agg_method(src_freq, target_freq, agg, rep.period, len(members), partial)
        flags = [f for f in rep.flags if f != "partial_period"]
        if partial:
            flags.append("partial_period")
        out.append(
            Observation(
                indicator=rep.indicator,
                iso=rep.iso,
                freq=target_freq,
                period=bucket,
                value=value,
                unit=rep.unit,
                source=rep.source,
                series_id=rep.series_id,
                source_url=rep.source_url,
                method=method,
                vintage=rep.vintage,
                flags=flags,
            )
        )
    return out


def _agg_method(
    src_freq: str, target_freq: str, agg: str, last_period: str, n: int, partial: bool
) -> str:
    tl = _TARGET_LABEL.get(target_freq, target_freq)
    sl = _FREQ_LABEL.get(src_freq, src_freq)
    if agg == "last":
        if src_freq in _DATE_LIKE:
            text = f"{tl}값 = {tl} 마지막 관측일({last_period}) 값"
        else:
            text = f"{tl}값 = {tl} 마지막 {sl}({last_period}) {sl}값"
    else:
        word = "평균" if agg == "mean" else "합계"
        text = f"{tl}{word} ({n}{_COUNT_UNIT.get(src_freq, '건')})"
    if partial:
        text += " · 진행 중 기간"
    return text


# ------------------------------------------------------------------
# 파생 계산
# ------------------------------------------------------------------
def yoy(
    obs: list[Observation],
    target_indicator: str | None = None,
    lag: int | None = None,
) -> list[Observation]:
    """전년비(%) 시계열. 같은 빈도의 12개월/4분기/1년 전 값과 비교한다.

    - `target_indicator`: 결과 지표 id (예: `cpi_yoy`). 생략하면 `*_index` -> `*_yoy`.
    - `lag`: 생략하면 빈도별 기본값(M 12, Q 4, Y 1, W 52). 일별(D)은 전년비를 만들지 않는다.
    """
    if not obs:
        return []
    ref = _require_homogeneous(obs)
    freq = ref.freq
    if freq not in DEFAULT_YOY_LAG:
        raise ValueError(f"{freq} 빈도는 전년비를 계산하지 않는다 (월/분기/연 단위만)")
    default_lag = DEFAULT_YOY_LAG[freq]
    use_lag = default_lag if lag is None else int(lag)
    if use_lag < 1:
        raise ValueError(f"lag는 1 이상이어야 한다 (현재 {use_lag})")
    out_indicator = target_indicator or _default_yoy_id(ref.indicator)

    by_period = {o.period: o for o in obs}
    out: list[Observation] = []
    for o in sorted(obs, key=lambda x: period_sort_key(x.freq, x.period)):
        base_period = previous_period(freq, o.period, use_lag)
        base = by_period.get(base_period)
        if o.value is None or base is None or not base.value:
            continue
        value = (float(o.value) / float(base.value) - 1.0) * 100.0
        method = f"전년비 = ({o.period} / {base_period} − 1) × 100"
        if use_lag != default_lag:
            method += f" · 시차 {use_lag}{_COUNT_UNIT.get(freq, '건')}"
        out.append(_derive(o, out_indicator, freq, o.period, value, "%", method))
    return out


def _default_yoy_id(indicator: str) -> str:
    if indicator.endswith("_index"):
        return f"{indicator[: -len('_index')]}_yoy"
    return f"{indicator}_yoy"


def index100(
    obs: list[Observation],
    base_period: str,
    target_indicator: str | None = None,
) -> list[Observation]:
    """기준 기간=100 지수. 기준 기간 관측이 없으면 가장 가까운 이전 관측을 쓴다."""
    if not obs:
        return []
    ref = _require_homogeneous(obs)
    used, base_value = _resolve_base(obs, base_period)
    out_indicator = target_indicator or ref.indicator
    note = _base_note(base_period, used)
    out: list[Observation] = []
    for o in sorted(obs, key=lambda x: period_sort_key(x.freq, x.period)):
        if o.value is None:
            continue
        value = float(o.value) / base_value * 100.0
        method = f"지수 = 값 / 기준값({used}{note}) × 100"
        out.append(_derive(o, out_indicator, o.freq, o.period, value, "index", method))
    return out


def fx_value_index(
    fx_obs: list[Observation],
    base_period: str,
    target_indicator: str = "fx_value_index",
) -> list[Observation]:
    """통화가치 지수 = 기준환율 / 환율 × 100.

    환율이 현지통화/USD이므로 값이 내려가면(=통화 강세) 지수는 올라간다.
    """
    if not fx_obs:
        return []
    _require_homogeneous(fx_obs)
    used, base_value = _resolve_base(fx_obs, base_period)
    note = _base_note(base_period, used)
    out: list[Observation] = []
    for o in sorted(fx_obs, key=lambda x: period_sort_key(x.freq, x.period)):
        if not o.value or float(o.value) <= 0:
            continue
        value = base_value / float(o.value) * 100.0
        method = f"통화가치지수 = 기준환율({used}{note}) / 환율 × 100"
        out.append(_derive(o, target_indicator, o.freq, o.period, value, "index", method))
    return out


def fuel_dependency(production: float | None, consumption: float | None) -> float | None:
    """연료별 해외의존도(%) = max(0, 1 - 생산/소비) × 100. 소비가 0 이하면 None."""
    if production is None or consumption is None:
        return None
    cons = float(consumption)
    if cons <= 0:
        return None
    return max(0.0, 1.0 - float(production) / cons) * 100.0


def _resolve_base(obs: list[Observation], base_period: str) -> tuple[str, float]:
    ref = obs[0]
    validate_period(ref.freq, base_period)
    target = period_sort_key(ref.freq, base_period)
    best: Observation | None = None
    for o in obs:
        if o.value is None:
            continue
        key = period_sort_key(o.freq, o.period)
        if key > target:
            continue
        if best is None or key > period_sort_key(best.freq, best.period):
            best = o
    if best is None or not best.value:
        raise ValueError(f"기준 기간 {base_period} 이전에 사용할 수 있는 관측치가 없다")
    return best.period, float(best.value)


def _base_note(requested: str, used: str) -> str:
    if requested == used:
        return ""
    return f", 요청 기준 {requested}의 최근접 이전 관측"


def _derive(
    src: Observation,
    indicator: str,
    freq: str,
    period: str,
    value: float,
    unit: str,
    method: str,
) -> Observation:
    flags = ["derived"]
    for f in src.flags:
        if f != "derived" and f not in flags:
            flags.append(f)
    return Observation(
        indicator=indicator,
        iso=src.iso,
        freq=freq,
        period=period,
        value=value,
        unit=unit,
        source="derived",
        series_id=f"{src.indicator}@{src.source}",
        source_url=src.source_url,
        method=method,
        vintage=src.vintage,
        flags=flags,
    )


def _require_homogeneous(obs: list[Observation]) -> Observation:
    ref = obs[0]
    for o in obs:
        if (o.indicator, o.iso, o.freq) != (ref.indicator, ref.iso, ref.freq):
            raise ValueError(
                "입력 관측치는 같은 indicator·iso·freq여야 한다 "
                f"({ref.indicator}/{ref.iso}/{ref.freq} vs {o.indicator}/{o.iso}/{o.freq})"
            )
    return ref


def _as_date(value: date | str | None) -> date:
    if value is None:
        return date.today()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


# ------------------------------------------------------------------
# 여론조사 평균 (poll of polls)
# ------------------------------------------------------------------
def poll_of_polls(
    polls: list[Doc],
    asof: str,
    ruling_party: str | None = None,
    window_days: int = 30,
) -> dict[str, Any] | None:
    """최근 window_days 조사의 표본크기·최근성 가중 평균 (CONTRACT 5장 payload).

    - 가중치 = 표본크기(없으면 1000 가정) × 최근성(asof 당일 1.0 → window_days 전 0.5, 선형)
    - 정당명은 입력 그대로 쓴다(정규화하지 않음). 정당별로 조사에 없으면 그 정당의
      가중합에서 제외하므로 결과 합이 100이 되지 않을 수 있다.
    - 창 안에 조사가 1건도 없으면 None.
    """
    if window_days <= 0:
        raise ValueError("window_days는 1 이상이어야 한다")
    asof_date = _as_date(asof)
    sums: dict[str, float] = {}
    weights: dict[str, float] = {}
    approval_sum = 0.0
    approval_weight = 0.0
    n_polls = 0

    for doc in polls:
        payload = doc.payload or {}
        results = payload.get("results") or {}
        if not isinstance(results, dict) or not results:
            continue
        poll_date = _as_date(payload.get("fieldwork_end") or doc.date)
        age = (asof_date - poll_date).days
        if age < 0 or age > window_days:
            continue
        sample = payload.get("sample_size")
        sample_n = float(sample) if sample else 1000.0
        weight = sample_n * (1.0 - 0.5 * age / window_days)
        if weight <= 0:
            continue
        n_polls += 1
        for party, pct in results.items():
            if pct is None:
                continue
            sums[party] = sums.get(party, 0.0) + weight * float(pct)
            weights[party] = weights.get(party, 0.0) + weight
        approval = payload.get("gov_approval")
        if approval is not None:
            approval_sum += weight * float(approval)
            approval_weight += weight

    if not n_polls or not weights:
        return None

    averaged = {p: round(sums[p] / weights[p], 2) for p in sums if weights.get(p)}
    leader_party = max(averaged, key=lambda p: averaged[p]) if averaged else None
    return {
        "asof": asof_date.isoformat(),
        "window_days": window_days,
        "results": averaged,
        "n_polls": n_polls,
        "ruling_party": ruling_party,
        "ruling_pct": averaged.get(ruling_party) if ruling_party else None,
        "leader_party": leader_party,
        "leader_pct": averaged.get(leader_party) if leader_party else None,
        "gov_approval": (
            round(approval_sum / approval_weight, 2) if approval_weight else None
        ),
    }


# ------------------------------------------------------------------
# 개요(LATEST) 집계
# ------------------------------------------------------------------
def median(values: list[float | None]) -> float | None:
    """None을 제외한 중앙값. 값이 없으면 None."""
    nums = sorted(float(v) for v in values if v is not None)
    if not nums:
        return None
    mid = len(nums) // 2
    if len(nums) % 2:
        return nums[mid]
    return (nums[mid - 1] + nums[mid]) / 2.0


def latest_by_country(obs_by_iso: dict[str, list[Observation]]) -> dict[str, dict[str, Any]]:
    """국가별 최신값 + 직전값·변화·순위 (CONTRACT 4장 `LATEST#<indicator>` 속성).

    `rank`는 value 내림차순 1부터이며 value가 None(복합값)인 국가는 순위에서 뺀다.
    `n`은 순위가 매겨진 국가 수로 모든 항목에 같은 값이 들어간다.
    """
    out: dict[str, dict[str, Any]] = {}
    for iso, obs in obs_by_iso.items():
        if not obs:
            continue
        ordered = sorted(obs, key=lambda o: period_sort_key(o.freq, o.period))
        latest = ordered[-1]
        prev = ordered[-2] if len(ordered) > 1 else None
        value = None if latest.value is None else float(latest.value)
        prev_value = None if prev is None or prev.value is None else float(prev.value)
        change = None
        change_pct = None
        if value is not None and prev_value is not None:
            change = value - prev_value
            if prev_value:
                change_pct = (value / prev_value - 1.0) * 100.0
        out[iso.upper()] = {
            "value": value,
            "payload": latest.payload,
            "period": latest.period,
            "freq": latest.freq,
            "unit": latest.unit,
            "source": latest.source,
            "flags": list(latest.flags),
            "prev_value": prev_value,
            "prev_period": prev.period if prev else None,
            "change": change,
            "change_pct": change_pct,
            "rank": None,
            "n": 0,
        }

    ranked = sorted(
        (iso for iso, row in out.items() if row["value"] is not None),
        key=lambda iso: out[iso]["value"],
        reverse=True,
    )
    for i, iso in enumerate(ranked, start=1):
        out[iso]["rank"] = i
    for row in out.values():
        row["n"] = len(ranked)
    return out
