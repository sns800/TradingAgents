# ============================================================================
# [모듈 개요 - 초보자용]
# 이 파일은 LLM 분석가가 숫자를 지어내지 못하도록, 실제 시장 데이터로부터
# "검증된 스냅샷(snapshot)"을 만들어 주는 모듈입니다. 최신
# OHLCV(시가·고가·저가·종가·거래량) 행, 주요 기술적 지표, 최근 종가 목록을
# 결정론적으로(LLM 개입 없이) 계산해 마크다운 표로 정리합니다.
# TradingAgents(LLM 멀티 에이전트 주식 트레이딩 프레임워크)에서 시장 분석가
# 에이전트에게 "정확한 수치의 유일한 출처"로 제공됩니다.
# ============================================================================

"""결정론적(deterministic) 시장 데이터 검증 스냅샷.

시장 분석가는 LLM이라 정확한 숫자를 지어낼(confabulate) 수 있습니다 —
기반 데이터가 뒷받침하지 않는 볼린저 밴드(Bollinger band) 값이나 "역사적으로
검증된 반등"을 인용하는 식으로요(#830). 이 모듈은 분석가가 모든 정확한
수치 주장에 대해 진실의 원천(source of truth)으로 삼도록 지시받는
근거 스냅샷(분석 날짜 이전의 최신 OHLCV 행, 공통 지표, 최근 종가)을
계산합니다. 결정론적이며 LLM은 관여하지 않습니다.
"""

from __future__ import annotations

from collections.abc import Iterable

import pandas as pd
from stockstats import wrap

from tradingagents.dataflows.stockstats_utils import load_ohlcv

# 스냅샷이 매 실행마다 같은 형태가 되도록 고정된 공통 지표 세트.
# EMA(지수이동평균), SMA(단순이동평균), RSI(상대강도지수),
# boll(볼린저 밴드), MACD(이동평균수렴확산), ATR(평균진폭) 등.
DEFAULT_SNAPSHOT_INDICATORS: tuple[str, ...] = (
    "close_10_ema", "close_50_sma", "close_200_sma",
    "rsi", "boll", "boll_ub", "boll_lb",
    "macd", "macds", "macdh", "atr",
)


def _verified_rows(symbol: str, curr_date: str) -> pd.DataFrame:
    """curr_date 이전(당일 포함)의 OHLCV를 날짜순으로 반환한다. 쓸 수 있는 데이터가 없으면 예외를 던진다.

    ``load_ohlcv``가 이미 Date 컬럼을 정규화하고 미래 데이터
    (look-ahead, 선견 편향) 행을 걸러내지만, 여기서 방어적으로 컷오프를
    다시 적용합니다 — 검증 경로이므로 입력이 미리 필터링되어 있다고
    믿어서는 안 되기 때문입니다.
    """
    data = load_ohlcv(symbol, curr_date)
    if data is None or data.empty:
        raise ValueError(f"No OHLCV data available for {symbol}.")

    df = data.copy()
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date"])
    df = df[df["Date"] <= pd.to_datetime(curr_date)].sort_values("Date")
    if df.empty:
        raise ValueError(f"No OHLCV rows on or before {curr_date} for {symbol}.")
    return df


def _fmt(value) -> str:
    # 값 유형별로 보기 좋은 문자열로 변환한다(없으면 "N/A").
    if value is None or pd.isna(value):
        return "N/A"
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int,)):
        return str(value)
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def build_verified_market_snapshot(
    symbol: str,
    curr_date: str,
    look_back_days: int = 30,
    indicators: Iterable[str] | None = None,
) -> str:
    """근거 스냅샷을 렌더링한다: 최신 OHLCV 행, 지표, 최근 종가."""
    # `df`는 원래의 대문자 OHLCV 컬럼(Open/High/Low/Close/Volume)을
    # 유지합니다; stockstats의 `wrap()`은 컬럼명을 소문자로 바꾸고 지표
    # 컬럼을 추가하므로, 원시 가격은 `df`에서, 지표는 `stock_df`에서 읽습니다.
    df = _verified_rows(symbol, curr_date)
    stock_df = wrap(df.copy())

    selected = tuple(indicators or DEFAULT_SNAPSHOT_INDICATORS)
    indicator_values: dict[str, str] = {}
    for name in selected:
        try:
            stock_df[name]  # stockstats의 지표 계산을 트리거한다
            indicator_values[name] = _fmt(stock_df.iloc[-1][name])
        except Exception as exc:  # noqa: BLE001 — 지표 하나가 잘못됐다고 스냅샷 전체가 실패해서는 안 된다
            indicator_values[name] = f"N/A ({type(exc).__name__})"

    latest = df.iloc[-1]
    latest_date = _fmt(latest["Date"])
    window = max(1, min(int(look_back_days), 30))
    recent = df.tail(window)

    lines = [
        f"## Verified market data snapshot for {symbol.upper()}",
        "",
        f"- Requested analysis date: {curr_date}",
        f"- Latest trading row used: {latest_date}",
        "- Rows after the requested analysis date are excluded before verification.",
        "",
        "### Latest verified OHLCV row",
        "",
        "| Field | Value |",
        "|---|---:|",
    ]
    for field in ("Open", "High", "Low", "Close", "Volume"):
        lines.append(f"| {field} | {_fmt(latest.get(field))} |")

    lines += ["", "### Verified technical indicators (latest row)", "",
              "| Indicator | Value |", "|---|---:|"]
    for name, value in indicator_values.items():
        lines.append(f"| {name} | {value} |")

    lines += ["", f"### Recent verified closes (last {len(recent)} rows)", "",
              "| Date | Close |", "|---|---:|"]
    for _, row in recent.iterrows():
        lines.append(f"| {_fmt(row['Date'])} | {_fmt(row.get('Close'))} |")

    lines += [
        "",
        "Use this snapshot as the source of truth for exact OHLCV, price-level, "
        "and indicator-value claims. If another tool output conflicts with it, "
        "flag the discrepancy rather than inventing a reconciled number. Do not "
        "claim historical validation, support/resistance bounces, or exact "
        "percentage moves unless directly supported by tool output with concrete "
        "dates and prices.",
    ]
    return "\n".join(lines)
