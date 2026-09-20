# ============================================================
# [모듈 개요] 국가별 시가총액 상위 기업 소스 (top_companies, 복합값 payload)
#
# 국가마다 시총 상위 10개 기업을 분기 관측치로 만듭니다 (CONTRACT.md 3장 복합값:
# `value=None`, `payload={"items":[...], "asof":..., "source_detail":...}`).
#
# [두 갈래 경로]
#  1) 한·일·미·중: 기존 종목 카탈로그(S3 `catalog/{KR|JP|US|CN}.json.gz`) 재사용.
#     webui/catalog/build_catalog.py의 항목 계약대로 item에
#     ticker/name/name_ko/market/sector/price/currency/market_cap이 있습니다.
#     주입점은 `ctx.extra["catalog_loader"]` — `callable(market) -> list[item]`.
#     USD 환산은 `ctx.extra["fx_rates"]`(`{"KRW": 1380.0, ...}` = 현지통화/USD)를
#     쓰고, 환율이 없으면 현지통화만 채우고 usd는 None으로 둡니다.
#  2) 나머지 국가: 대표 지수 구성 종목 티커를 모듈 상수(INDEX_TICKERS)로 두고
#     yfinance `Ticker.fast_info["marketCap"]`(실패 시 `.info`)로 시총을 조회한
#     뒤 상위 10개를 고릅니다. 종목 단위 실패는 건너뜁니다.
#
# [실측으로 확인한 것] (2026-09-20, yfinance fast_info)
#   RELIANCE.NS 1.66e13 INR · SAP.DE 2.11e11 EUR · SHEL.L 2.02e13 **GBp** ·
#   MC.PA 1.99e11 EUR · PETR4.SA 6.63e11 BRL · 2222.SR 6.18e12 SAR ·
#   NPN.JO 5.35e13 **ZAc** · BHP.AX 3.10e11 AUD · WALMEX.MX 7.98e11 MXN ·
#   BBCA.JK 7.74e14 IDR · THYAO.IS 3.92e11 TRY · YPFD.BA 3.43e13 ARS ·
#   RY.TO 3.94e11 CAD · ENI.MI 6.88e10 EUR  (14/14 성공)
#   → 런던(GBp=펜스)·요하네스버그(ZAc=센트)는 **보조통화** 단위라 100으로 나눠야
#     합니다(MINOR_UNIT_CURRENCIES). 그대로 쓰면 시총이 100배가 됩니다.
#   러시아: Yahoo가 MOEX 시세를 제공하지 않아 티커 목록이 비어 있습니다
#     → 빈 결과 + 로그 (NO_TICKER_NOTE).
#   유로존(EU): 단일 거래소가 없어 건너뜁니다 (DE/FR/IT가 대표).
#
# [source 필드] CONTRACT 3장의 source 열거에는 `companies`가 없고 `catalog`·
# `yahoo`가 있습니다. 모듈/INGEST 식별자는 CONTRACT 12장 표대로 `companies`,
# 개별 관측치의 source는 경로에 따라 `catalog` 또는 `yahoo`를 씁니다.
#
# [타임아웃 주의] yfinance 내부 HTTP에는 타임아웃이 없어 배치가 멈출 수 있습니다.
# 진입점(collect.py)에서 build_catalog.py와 같이 `socket.setdefaulttimeout(...)`을
# 걸어 주세요. 이 모듈은 라이브러리라 전역 설정을 건드리지 않습니다.
#
# 국가별 실패 격리: 국가 하나의 로더/조회 실패는 ctx.record_error(...)로 기록만
# 하고 다음 국가로 넘어갑니다.
# ============================================================
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from macro.schema import Observation
from macro.sources.base import (
    CollectContext,
    field_of,
    find_indicator,
    iter_countries,
    safe_float,
)

SOURCE_NAME = "companies"
CADENCE = "quarterly"

# 관측치의 source 필드 (CONTRACT 3장 열거)
SOURCE_CATALOG = "catalog"
SOURCE_YAHOO = "yahoo"

TOP_N = 10

# 카탈로그가 있는 시장 (webui/catalog/build_catalog.py MARKETS)
CATALOG_MARKETS: dict[str, str] = {"KR": "KR", "JP": "JP", "US": "US", "CN": "CN"}

# ctx.extra 주입점
EXTRA_CATALOG_LOADER = "catalog_loader"
EXTRA_FX_RATES = "fx_rates"

# 보조통화 표기 → (주통화, 나눌 값). Yahoo가 펜스·센트로 주는 시장을 보정한다.
# 키 비교는 대소문자를 구분한다 — "GBP"(파운드)와 "GBp"(펜스)는 다른 단위다.
MINOR_UNIT_CURRENCIES: dict[str, tuple[str, float]] = {
    "GBp": ("GBP", 100.0),   # 런던증권거래소: 펜스
    "ZAc": ("ZAR", 100.0),   # 요하네스버그: 센트
    "ILA": ("ILS", 100.0),   # 텔아비브: 아고로트 (참고용)
}

NO_TICKER_NOTE = "Yahoo Finance가 시세를 제공하지 않아 대표 종목 목록이 없습니다"

METHOD_CATALOG = (
    "기존 종목 카탈로그(catalog/{market}.json.gz)의 시가총액 상위 {top}개. "
    "USD 환산은 수집 시점 환율(현지통화/USD) 적용"
)
METHOD_YAHOO = (
    "대표 지수 구성 종목 후보 {n}개의 yfinance 시가총액을 조회해 상위 {top}개 선정 "
    "(비공식 소스, 참고용). 펜스·센트 표기 시장은 주통화로 환산"
)

SUPPORTED: tuple[str, ...] = ("top_companies",)

# 카탈로그가 없는 국가의 대표 지수 구성 종목 (Yahoo 티커, 국가별 15개 내외).
# 시총 상위는 조회 결과로 정렬하므로 순서는 의미 없습니다.
INDEX_TICKERS: dict[str, tuple[str, ...]] = {
    "IN": (  # NIFTY 50
        "RELIANCE.NS", "HDFCBANK.NS", "TCS.NS", "BHARTIARTL.NS", "ICICIBANK.NS",
        "INFY.NS", "SBIN.NS", "LICI.NS", "ITC.NS", "HINDUNILVR.NS",
        "LT.NS", "BAJFINANCE.NS", "MARUTI.NS", "SUNPHARMA.NS", "KOTAKBANK.NS",
    ),
    "DE": (  # DAX
        "SAP.DE", "SIE.DE", "ALV.DE", "DTE.DE", "MUV2.DE",
        "AIR.DE", "MRK.DE", "RHM.DE", "BAS.DE", "BMW.DE",
        "MBG.DE", "DBK.DE", "IFX.DE", "ADS.DE", "VOW3.DE",
    ),
    "GB": (  # FTSE 100
        "SHEL.L", "AZN.L", "HSBA.L", "ULVR.L", "RIO.L",
        "BP.L", "GSK.L", "REL.L", "LSEG.L", "BATS.L",
        "DGE.L", "NG.L", "GLEN.L", "RR.L", "BARC.L",
    ),
    "FR": (  # CAC 40
        "MC.PA", "OR.PA", "RMS.PA", "TTE.PA", "SAN.PA",
        "AIR.PA", "SU.PA", "AI.PA", "EL.PA", "BNP.PA",
        "CS.PA", "DG.PA", "SAF.PA", "KER.PA", "CAP.PA",
    ),
    "IT": (  # FTSE MIB
        "ENI.MI", "ISP.MI", "UCG.MI", "ENEL.MI", "G.MI",
        "STLAM.MI", "FBK.MI", "RACE.MI", "PST.MI", "TRN.MI",
        "SRG.MI", "MONC.MI", "PRY.MI", "LDO.MI", "TIT.MI",
    ),
    "CA": (  # S&P/TSX 60
        "RY.TO", "TD.TO", "ENB.TO", "CNR.TO", "BMO.TO",
        "BNS.TO", "CP.TO", "SU.TO", "TRP.TO", "CNQ.TO",
        "BAM.TO", "MFC.TO", "ATD.TO", "SHOP.TO", "WCN.TO",
    ),
    "AU": (  # S&P/ASX 50
        "BHP.AX", "CBA.AX", "CSL.AX", "NAB.AX", "WBC.AX",
        "ANZ.AX", "WES.AX", "MQG.AX", "RIO.AX", "WOW.AX",
        "TLS.AX", "GMG.AX", "FMG.AX", "WDS.AX", "ALL.AX",
    ),
    "BR": (  # Ibovespa
        "PETR4.SA", "VALE3.SA", "ITUB4.SA", "BBDC4.SA", "ABEV3.SA",
        "BBAS3.SA", "WEGE3.SA", "B3SA3.SA", "ELET3.SA", "RENT3.SA",
        "SUZB3.SA", "PRIO3.SA", "RADL3.SA", "JBSS3.SA", "EQTL3.SA",
    ),
    "MX": (  # S&P/BMV IPC
        "WALMEX.MX", "AMXB.MX", "GFNORTEO.MX", "FEMSAUBD.MX", "GMEXICOB.MX",
        "CEMEXCPO.MX", "BIMBOA.MX", "TLEVISACPO.MX", "ASURB.MX", "GAPB.MX",
        "KOFUBL.MX", "ALSEA.MX", "ELEKTRA.MX", "PINFRA.MX", "OMAB.MX",
    ),
    "ID": (  # IDX30
        "BBCA.JK", "BBRI.JK", "BMRI.JK", "TLKM.JK", "ASII.JK",
        "BBNI.JK", "TPIA.JK", "ICBP.JK", "UNVR.JK", "ADRO.JK",
        "KLBF.JK", "AMRT.JK", "INDF.JK", "GOTO.JK", "ANTM.JK",
    ),
    "TR": (  # BIST 30
        "THYAO.IS", "ASELS.IS", "BIMAS.IS", "AKBNK.IS", "GARAN.IS",
        "ISCTR.IS", "KCHOL.IS", "SAHOL.IS", "EREGL.IS", "TUPRS.IS",
        "FROTO.IS", "TCELL.IS", "SISE.IS", "PGSUS.IS", "YKBNK.IS",
    ),
    "SA": (  # Tadawul All Share
        "2222.SR", "1120.SR", "2010.SR", "7010.SR", "1180.SR",
        "1010.SR", "2380.SR", "1211.SR", "2020.SR", "1050.SR",
        "5110.SR", "4001.SR", "2280.SR", "1060.SR", "4013.SR",
    ),
    "ZA": (  # JSE Top 40
        "NPN.JO", "PRX.JO", "BTI.JO", "FSR.JO", "SBK.JO",
        "CPI.JO", "ABG.JO", "GFI.JO", "AGL.JO", "MTN.JO",
        "VOD.JO", "SLM.JO", "BID.JO", "IMP.JO", "ANG.JO",
    ),
    "AR": (  # S&P Merval
        "YPFD.BA", "GGAL.BA", "BBAR.BA", "PAMP.BA", "TXAR.BA",
        "ALUA.BA", "CRES.BA", "LOMA.BA", "SUPV.BA", "BMA.BA",
        "TGSU2.BA", "CEPU.BA", "COME.BA", "EDN.BA", "TRAN.BA",
    ),
    # 러시아: Yahoo가 MOEX를 제공하지 않음. 유로존: 단일 거래소 없음.
    "RU": (),
    "EU": (),
}


# ====================================================================== 공개 함수
def current_quarter(today: date | None = None) -> str:
    """CONTRACT 3장 분기 기간 문자열 `YYYY-Qn`."""
    d = today or date.today()
    return f"{d.year:04d}-Q{(d.month - 1) // 3 + 1}"


def normalize_currency(currency: str | None, market_cap: float | None) -> tuple[str, float | None]:
    """보조통화(펜스·센트) 표기를 주통화로 바꾼다.

    `("GBp", 2.02e13)` → `("GBP", 2.02e11)`. 표에 없으면 대문자 그대로.
    """
    raw = (currency or "").strip()
    if raw in MINOR_UNIT_CURRENCIES:
        major, divisor = MINOR_UNIT_CURRENCIES[raw]
        return major, (market_cap / divisor if market_cap is not None else None)
    return raw.upper(), market_cap


def to_usd_bn(market_cap: float | None, currency: str, fx_rates: dict[str, float]) -> float | None:
    """현지통화 시총 → USD 10억. 환율(현지통화/USD)이 없으면 None."""
    if market_cap is None:
        return None
    if currency == "USD":
        return market_cap / 1e9
    rate = safe_float(fx_rates.get(currency))
    if not rate:
        return None
    return market_cap / rate / 1e9


def collect(
    countries: list[Any],
    indicators: list[Any],
    ctx: CollectContext,
) -> list[Observation]:
    """국가별 시총 상위 10개 기업을 분기 관측치로 만든다 (CONTRACT 7장)."""
    if not _is_wanted(indicators, "top_companies"):
        return []
    ind = find_indicator(indicators, "top_companies") or _FallbackIndicator("top_companies")
    period = current_quarter()
    asof = datetime.now(timezone.utc).isoformat(timespec="seconds")
    fx_rates = _fx_rates(ctx)
    loader = ctx.extra.get(EXTRA_CATALOG_LOADER)
    if not callable(loader):
        ctx.log(
            f"[{SOURCE_NAME}] ctx.extra['{EXTRA_CATALOG_LOADER}'] 없음 — "
            "한·일·미·중도 yfinance 폴백을 씁니다"
        )
        loader = None
    if not ctx.extra.get(EXTRA_FX_RATES):
        ctx.log(f"[{SOURCE_NAME}] ctx.extra['{EXTRA_FX_RATES}'] 없음 — USD 환산 없이 현지통화만 저장")

    obs: list[Observation] = []
    for country in iter_countries(countries, ind, SOURCE_NAME):
        iso = str(field_of(country, "iso") or "").upper()
        if not iso:
            continue
        try:
            ob = _country_obs(ctx, iso, period, asof, fx_rates, loader)
        except Exception as exc:  # noqa: BLE001 - 국가별 실패 격리
            ctx.record_error(SOURCE_NAME, iso, f"top_companies 수집 실패: {exc}")
            continue
        if ob is not None:
            obs.append(ob)
    _log_coverage(ctx, countries, ind, obs)
    return obs


# ====================================================================== 내부 구현
def _country_obs(
    ctx: CollectContext,
    iso: str,
    period: str,
    asof: str,
    fx_rates: dict[str, float],
    loader: Any,
) -> Observation | None:
    """한 국가의 top_companies Observation (만들 수 없으면 None)."""
    market = CATALOG_MARKETS.get(iso)
    if market and loader is not None:
        items = _from_catalog(ctx, market, fx_rates)
        if items:
            return _build(
                iso,
                period,
                items,
                asof,
                detail="catalog",
                source=SOURCE_CATALOG,
                series_id=f"catalog/{market}.json.gz",
                url=f"catalog/{market}.json.gz",
                method=METHOD_CATALOG.format(market=market, top=TOP_N),
            )
        ctx.log(f"[{SOURCE_NAME}] {iso} 카탈로그 비어 있음 — yfinance 폴백")

    tickers = INDEX_TICKERS.get(iso)
    if tickers is None:
        ctx.log(f"[{SOURCE_NAME}] {iso} 대표 종목 목록 미등록 — 건너뜀")
        return None
    if not tickers:
        ctx.log(f"[{SOURCE_NAME}] {iso} {NO_TICKER_NOTE} — 빈 결과")
        return None

    items = _from_yfinance(ctx, iso, tickers, fx_rates)
    if not items:
        return None
    return _build(
        iso,
        period,
        items,
        asof,
        detail="yfinance",
        source=SOURCE_YAHOO,
        series_id=f"yfinance/marketCap/{iso}",
        url="https://finance.yahoo.com/",
        method=METHOD_YAHOO.format(n=len(tickers), top=TOP_N),
    )


def _from_catalog(
    ctx: CollectContext,
    market: str,
    fx_rates: dict[str, float],
) -> list[dict[str, Any]]:
    """카탈로그 항목에서 시총 상위 TOP_N개 payload item을 만든다."""
    loader = ctx.extra[EXTRA_CATALOG_LOADER]
    rows = loader(market) or []
    scored: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        cap = safe_float(field_of(row, "market_cap"))
        if cap is None or cap <= 0:
            continue
        currency, cap = normalize_currency(field_of(row, "currency"), cap)
        name = field_of(row, "name_ko") or field_of(row, "name") or ""
        scored.append(
            (
                cap,
                _item(name, str(field_of(row, "ticker") or ""), field_of(row, "sector"),
                      cap, currency, fx_rates),
            )
        )
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in scored[:TOP_N]]


def _from_yfinance(
    ctx: CollectContext,
    iso: str,
    tickers: tuple[str, ...],
    fx_rates: dict[str, float],
) -> list[dict[str, Any]]:
    """yfinance로 시총을 조회해 상위 TOP_N개 payload item을 만든다."""
    try:
        import yfinance as yf
    except ImportError as exc:  # pragma: no cover - 워커 이미지에는 포함됨
        ctx.record_error(SOURCE_NAME, iso, f"yfinance 임포트 실패: {exc}")
        return []

    scored: list[tuple[float, dict[str, Any]]] = []
    for ticker in tickers:
        try:
            cap, currency, name, sector = _yf_snapshot(yf, ticker)
        except Exception:  # noqa: BLE001 - 종목 단위 실패는 조용히 건너뛴다
            continue
        if cap is None or cap <= 0:
            continue
        currency, cap = normalize_currency(currency, cap)
        scored.append((cap, _item(name or ticker, ticker, sector, cap, currency, fx_rates)))
    if len(scored) < len(tickers):
        ctx.log(f"[{SOURCE_NAME}] {iso} 시총 조회 {len(scored)}/{len(tickers)}건 성공")
    scored.sort(key=lambda pair: pair[0], reverse=True)
    top = [item for _, item in scored[:TOP_N]]
    _enrich_names(yf, top)
    return top


def _item(
    name: str,
    ticker: str,
    sector: Any,
    market_cap_local: float,
    currency: str,
    fx_rates: dict[str, float],
) -> dict[str, Any]:
    """payload item 1개.

    `label`/`value`는 복합값 3종(elec_mix·exports_top_hs2·top_companies)이 공유하는
    공통 쌍이라 프론트가 한 규칙으로 렌더할 수 있게 함께 넣는다
    (registry.yaml의 top_companies 주석도 label/value를 가리킨다).
    """
    usd_bn = _round(to_usd_bn(market_cap_local, currency, fx_rates), 4)
    return {
        "label": name,
        "value": usd_bn,
        "name": name,
        "ticker": ticker,
        "sector": sector or None,
        "market_cap_usd_bn": usd_bn,
        "market_cap_local": _round(market_cap_local, 2),
        "currency": currency or None,
    }


def _enrich_names(yf: Any, items: list[dict[str, Any]]) -> None:
    """상위 항목에만 `.info`를 한 번 더 호출해 회사명·업종을 채운다.

    `fast_info`에는 이름·업종이 없어 티커만 남으므로, 정렬이 끝난 상위 10개에
    대해서만 추가 조회한다(요청 수 절감). 실패한 종목은 티커를 그대로 둔다.
    """
    for item in items:
        if item.get("sector") and item.get("name") != item.get("ticker"):
            continue
        try:
            info = yf.Ticker(item["ticker"]).info or {}
        except Exception:  # noqa: BLE001 - 이름 보강 실패는 무해하다
            continue
        name = info.get("shortName") or info.get("longName")
        if name:
            item["name"] = item["label"] = name
        if info.get("sector"):
            item["sector"] = info["sector"]


def _yf_snapshot(yf: Any, ticker: str) -> tuple[float | None, str, str | None, str | None]:
    """`fast_info`를 먼저 보고, 시총이 없으면 `.info`로 한 번 더 시도한다."""
    handle = yf.Ticker(ticker)
    cap: float | None = None
    currency = ""
    try:
        fast = handle.fast_info
        cap = safe_float(fast.get("marketCap"))
        currency = str(fast.get("currency") or "")
    except Exception:  # noqa: BLE001 - fast_info는 자주 비어 있다
        pass
    name: str | None = None
    sector: str | None = None
    if cap is None or not currency:
        info = handle.info or {}
        cap = cap if cap is not None else safe_float(info.get("marketCap"))
        currency = currency or str(info.get("currency") or "")
        name = info.get("shortName") or info.get("longName")
        sector = info.get("sector")
    return cap, currency, name, sector


def _build(
    iso: str,
    period: str,
    items: list[dict[str, Any]],
    asof: str,
    detail: str,
    source: str,
    series_id: str,
    url: str,
    method: str,
) -> Observation:
    """복합값 Observation 1건을 만든다 (CONTRACT 3장)."""
    payload: dict[str, Any] = {"items": items, "asof": asof, "source_detail": detail}
    return Observation(
        indicator="top_companies",
        iso=iso,
        freq="Q",
        period=period,
        value=None,
        payload=payload,
        unit="usd_bn",
        source=source,
        series_id=series_id,
        source_url=url,
        method=method,
        flags=["partial_period"],
    )


def _fx_rates(ctx: CollectContext) -> dict[str, float]:
    """`ctx.extra["fx_rates"]`를 `{통화: 현지통화/USD}`로 정규화한다."""
    raw = ctx.extra.get(EXTRA_FX_RATES) or {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for key, value in raw.items():
        rate = safe_float(value)
        if rate:
            out[str(key).upper()] = rate
    out.setdefault("USD", 1.0)
    return out


def _round(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


def _is_wanted(indicators: list[Any], indicator_id: str) -> bool:
    """요청 지표 목록이 비어 있으면 전 지원 지표, 아니면 목록에 있는 것만."""
    if not indicators:
        return True
    return find_indicator(indicators, indicator_id) is not None


class _FallbackIndicator:
    """registry 항목 없이 지표 id만 아는 경우의 최소 대역(iter_countries용)."""

    def __init__(self, indicator_id: str) -> None:
        self.id = indicator_id
        self.sources: list[dict[str, Any]] = []


def _log_coverage(
    ctx: CollectContext,
    countries: list[Any],
    ind: Any,
    obs: list[Observation],
) -> None:
    """값을 못 만든 국가를 한 줄로 알린다."""
    target = {
        str(field_of(c, "iso") or "").upper() for c in iter_countries(countries, ind, SOURCE_NAME)
    } - {""}
    missing = sorted(target - {ob.iso for ob in obs})
    if missing:
        ctx.log(f"[{SOURCE_NAME}] top_companies 미수집 {len(missing)}개국: {','.join(missing)}")
