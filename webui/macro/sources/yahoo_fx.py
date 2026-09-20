# ============================================================
# [모듈 개요] Yahoo Finance 일별 환율·달러지수 정량 소스
#
# 엔드포인트(비공식):
#   https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=10y
#   User-Agent가 없으면 429를 돌려주므로 ctx.http의 세션 UA가 필수다.
# yfinance(설치돼 있음)를 2차 폴백으로 둔다 — chart API가 차단되면 동일한
# 일별 종가를 yfinance.Ticker().history()로 받는다.
#
# 응답 구조(2026-09-20 실측):
#   chart.result[0].meta.{gmtoffset, exchangeTimezoneName, regularMarketTime}
#   chart.result[0].timestamp[]                 거래소 로컬 자정의 UTC epoch
#   chart.result[0].indicators.quote[0].close[] 일별 종가 (None 가능)
# → 날짜는 `utcfromtimestamp(ts + gmtoffset).date()`로 복원해야 한다
#   (예 ts=1789340400, gmtoffset=3600 → 2026-09-14). 당일 마지막 점은 장중
#   실시간 시세일 수 있어 같은 날짜가 중복되면 마지막 값으로 덮어쓴다.
#
# [빈도 정책 — 다른 에이전트 주의]
# 이 모듈은 **freq D만** 저장한다. fx_usd 월별(freq M)은 BIS(WS_XRU, 기말)가
# 1차 소스이며, OBS 키가 `OBS#fx_usd#<iso>` / `M#YYYY-MM`으로 동일해 소스가
# 달라도 서로 덮어쓰기 때문이다. BIS가 실패했을 때 collect.py(수집기)가 이
# 모듈의 D 시계열을 D→M 기말 집계해 폴백으로 채운다. 이 모듈은 우선순위를
# 판단하지 않으므로 `fallback_source` 플래그도 붙이지 않는다.
#
# [심볼 규약 — 2026-09-20 전 종목 200 확인]
#   `{CCY}=X`   USD/현지통화 = lcu_per_usd  → invert=False
#               KRW=X JPY=X CNY=X INR=X IDR=X BRL=X MXN=X ARS=X TRY=X SAR=X
#               ZAR=X RUB=X CAD=X
#   `{CCY}USD=X` 현지통화/USD             → invert=True (1/price)
#               EURUSD=X GBPUSD=X AUDUSD=X
#   `DX-Y.NYB`  ICE 달러지수 → indicator `dxy`, iso "US", unit index
# ============================================================
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any

from macro.schema import Observation
from macro.sources.base import (
    CollectContext,
    country_code,
    field_of,
    find_indicator,
    indicator_unit,
    iter_countries,
    safe_float,
)

SOURCE_NAME = "yahoo"
CADENCE = "daily"

CHART_BASE = "https://query1.finance.yahoo.com/v8/finance/chart"
DEFAULT_RANGE = "10y"
DXY_SYMBOL = "DX-Y.NYB"
DXY_ISO = "US"

# registry가 codes["yahoo_fx"]를 주지 않을 때의 폴백 (iso → {symbol, invert})
DEFAULT_SYMBOLS: dict[str, dict[str, Any]] = {
    "KR": {"symbol": "KRW=X", "invert": False},
    "JP": {"symbol": "JPY=X", "invert": False},
    "CN": {"symbol": "CNY=X", "invert": False},
    "EU": {"symbol": "EURUSD=X", "invert": True},
    "GB": {"symbol": "GBPUSD=X", "invert": True},
    "CA": {"symbol": "CAD=X", "invert": False},
    "AU": {"symbol": "AUDUSD=X", "invert": True},
    "IN": {"symbol": "INR=X", "invert": False},
    "ID": {"symbol": "IDR=X", "invert": False},
    "BR": {"symbol": "BRL=X", "invert": False},
    "MX": {"symbol": "MXN=X", "invert": False},
    "AR": {"symbol": "ARS=X", "invert": False},
    "TR": {"symbol": "TRY=X", "invert": False},
    "SA": {"symbol": "SAR=X", "invert": False},
    "ZA": {"symbol": "ZAR=X", "invert": False},
    "RU": {"symbol": "RUB=X", "invert": False},
}


def collect(countries: list, indicators: list, ctx: CollectContext) -> list[Observation]:
    """Yahoo에서 일별 대미환율(fx_usd)과 달러지수(dxy)를 수집한다.

    국가별 실패 격리: 심볼 하나가 실패하면 record_error 후 다음 심볼로 넘어간다.
    비공식 API라 전부 실패할 수 있으며, 그때도 예외 없이 빈 리스트를 돌려준다.
    """
    out: list[Observation] = []
    rng = _range(ctx.since)

    fx_indicator = find_indicator(indicators, "fx_usd")
    if fx_indicator is not None:
        unit = indicator_unit(fx_indicator, "lcu_per_usd")
        for country in iter_countries(countries, fx_indicator, SOURCE_NAME):
            iso = str(field_of(country, "iso") or "").upper()
            if iso == "US":
                continue  # 기준통화, 정의상 1.0
            spec = _symbol_spec(country, iso)
            if not spec:
                ctx.log(f"[{SOURCE_NAME}] {iso}: yahoo_fx 심볼 미정의 — 건너뜀")
                continue
            symbol, invert = spec["symbol"], bool(spec.get("invert"))
            series = _daily_closes(symbol, rng, ctx, iso)
            if not series:
                continue
            for period, price in series:
                value = (1.0 / price) if invert else price
                if invert and price == 0:
                    continue
                out.append(
                    Observation(
                        indicator="fx_usd",
                        iso=iso,
                        freq="D",
                        period=period,
                        value=value,
                        unit=unit,
                        source=SOURCE_NAME,
                        series_id=symbol,
                        source_url=f"https://finance.yahoo.com/quote/{symbol}",
                        method=(
                            "일별 종가 역수 (Yahoo, 현지통화/USD → USD/현지통화)"
                            if invert
                            else "일별 종가 (Yahoo, USD/현지통화)"
                        ),
                    )
                )

    dxy_indicator = find_indicator(indicators, "dxy")
    # dxy는 registry에서 `only: [US]`이고 iso="US"로 저장한다(CONTRACT 2장).
    if dxy_indicator is not None and _us_requested(countries):
        unit = indicator_unit(dxy_indicator, "index")
        series = _daily_closes(DXY_SYMBOL, rng, ctx, DXY_ISO)
        for period, price in series:
            out.append(
                Observation(
                    indicator="dxy",
                    iso=DXY_ISO,
                    freq="D",
                    period=period,
                    value=price,
                    unit=unit,
                    source=SOURCE_NAME,
                    series_id=DXY_SYMBOL,
                    source_url=f"https://finance.yahoo.com/quote/{DXY_SYMBOL}",
                    method="ICE 달러지수 일별 종가 (Yahoo)",
                )
            )
    return out


# ====================================================================== 내부
def _us_requested(countries: list) -> bool:
    if not countries:
        return True
    return any(str(field_of(c, "iso") or "").upper() == "US" for c in countries)


def _range(since: date | None) -> str:
    """since를 Yahoo의 range 파라미터로 바꾼다 (chart API는 range가 더 안정적)."""
    if since is None:
        return DEFAULT_RANGE
    days = (datetime.now(timezone.utc).date() - since).days
    for limit, label in ((5, "5d"), (30, "1mo"), (95, "3mo"), (185, "6mo"), (370, "1y")):
        if days <= limit:
            return label
    for limit, label in ((740, "2y"), (1850, "5y"), (3700, "10y")):
        if days <= limit:
            return label
    return "max"


def _symbol_spec(country: Any, iso: str) -> dict[str, Any] | None:
    spec = country_code(country, "yahoo_fx")
    if isinstance(spec, dict) and spec.get("symbol"):
        return spec
    if isinstance(spec, str) and spec:
        return {"symbol": spec, "invert": False}
    return DEFAULT_SYMBOLS.get(iso)


def _daily_closes(
    symbol: str, rng: str, ctx: CollectContext, iso: str
) -> list[tuple[str, float]]:
    """chart API → (실패 시) yfinance 순으로 (YYYY-MM-DD, close)를 얻는다."""
    rows = _chart_api(symbol, rng, ctx, iso)
    if rows:
        return rows
    return _yfinance_fallback(symbol, rng, ctx, iso)


def _chart_api(symbol: str, rng: str, ctx: CollectContext, iso: str) -> list[tuple[str, float]]:
    url = f"{CHART_BASE}/{symbol}"
    try:
        resp = ctx.get(url, params={"interval": "1d", "range": rng})
    except Exception as exc:  # noqa: BLE001 - 비공식 API 실패는 격리
        ctx.record_error(SOURCE_NAME, iso, f"{symbol} chart 요청 실패: {exc}")
        return []
    if resp.status_code != 200:
        ctx.record_error(SOURCE_NAME, iso, f"{symbol} chart HTTP {resp.status_code}")
        return []
    text = resp.text
    ctx.save_raw(SOURCE_NAME, f"chart_{symbol}", text)
    try:
        doc = json.loads(text)
    except (ValueError, json.JSONDecodeError) as exc:
        ctx.record_error(SOURCE_NAME, iso, f"{symbol} chart JSON 파싱 실패: {exc}")
        return []
    return parse_chart(doc, ctx=ctx, iso=iso, symbol=symbol)


def parse_chart(
    doc: dict[str, Any],
    ctx: CollectContext | None = None,
    iso: str = "",
    symbol: str = "",
) -> list[tuple[str, float]]:
    """Yahoo chart JSON → [(YYYY-MM-DD, close)] 기간 오름차순.

    거래소 로컬 자정 기준으로 날짜를 복원하고(gmtoffset 보정), close가 None인
    점(휴장)과 중복 날짜(장중 실시간 시세)를 정리한다. 테스트가 직접 호출한다.
    """
    chart = (doc or {}).get("chart") or {}
    err = chart.get("error")
    if err:
        if ctx is not None:
            ctx.record_error(SOURCE_NAME, iso, f"{symbol} chart 오류: {err}")
        return []
    results = chart.get("result") or []
    if not results:
        if ctx is not None:
            ctx.record_error(SOURCE_NAME, iso, f"{symbol} chart 결과 없음")
        return []
    node = results[0] or {}
    meta = node.get("meta") or {}
    offset = int(meta.get("gmtoffset") or 0)
    stamps = node.get("timestamp") or []
    quotes = (node.get("indicators") or {}).get("quote") or [{}]
    closes = (quotes[0] or {}).get("close") or []
    by_date: dict[str, float] = {}
    for idx, ts in enumerate(stamps):
        close = safe_float(closes[idx]) if idx < len(closes) else None
        if close is None:
            continue
        try:
            day = datetime.fromtimestamp(int(ts) + offset, tz=timezone.utc).date()
        except (OverflowError, OSError, TypeError, ValueError):
            continue
        by_date[day.isoformat()] = close  # 같은 날짜는 나중 값(최신)으로 덮어쓴다
    return [(d, by_date[d]) for d in sorted(by_date)]


def _yfinance_fallback(
    symbol: str, rng: str, ctx: CollectContext, iso: str
) -> list[tuple[str, float]]:
    """chart API가 막혔을 때의 2차 경로. yfinance가 없으면 빈 리스트."""
    try:
        import yfinance  # 지연 임포트: 폴백 경로에서만 필요
    except ImportError:
        return []
    try:
        hist = yfinance.Ticker(symbol).history(period=rng, interval="1d", auto_adjust=False)
    except Exception as exc:  # noqa: BLE001 - 폴백 실패도 격리
        ctx.record_error(SOURCE_NAME, iso, f"{symbol} yfinance 실패: {exc}")
        return []
    if hist is None or getattr(hist, "empty", True):
        return []
    out: list[tuple[str, float]] = []
    for idx, row in hist.iterrows():
        value = safe_float(row.get("Close"))
        if value is None:
            continue
        out.append((idx.date().isoformat(), value))
    out.sort(key=lambda p: p[0])
    ctx.log(f"[{SOURCE_NAME}] {iso}: {symbol} yfinance 폴백으로 {len(out)}건 수집")
    return out
