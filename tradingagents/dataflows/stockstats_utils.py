# ============================================================================
# [모듈 개요 - 초보자용]
# 이 파일은 야후 파이낸스(Yahoo Finance)에서 OHLCV(시가·고가·저가·종가·거래량)
# 주가 데이터를 내려받아 CSV로 캐시(cache)하고, stockstats 라이브러리로
# 기술적 지표를 계산하는 핵심 데이터 적재 모듈입니다. 캐시 신선도 검사,
# 오래된(stale) 데이터 거부, 미래 데이터 차단(look-ahead 방지) 같은
# 안전장치를 담고 있습니다. TradingAgents(LLM 멀티 에이전트 주식 트레이딩
# 프레임워크)의 시장 분석 도구 대부분이 이 파일의 load_ohlcv()를 통해
# 가격 데이터를 얻습니다.
# ============================================================================

import logging
import os
import time
from typing import Annotated

import pandas as pd
import yfinance as yf
from stockstats import wrap
from yfinance.exceptions import YFRateLimitError

from .config import get_config
from .symbol_utils import NoMarketDataError, normalize_symbol
from .utils import safe_ticker_component

logger = logging.getLogger(__name__)

# 벤더가 준 최신 OHLCV 행이 요청 날짜보다 이 달력일 수만큼 오래됐으면
# 진부(stale)한 것으로 간주합니다. 긴 연휴 주말을 넘길 만큼 관대하면서도,
# yfinance가 간혹 반환하는 1년 묵은 프레임(#1021)은 잡아낼 만큼 엄격한 값.
MAX_OHLCV_STALE_DAYS = 10

# 요청한 날짜에 아직 도달하지 못한 당일 캐시를 다시 받기 전까지 재사용할
# 수 있는 시간(#1150). 장중 실행이 당일 종가 발표 직후 곧 그것을 집어 올
# 만큼 짧고, 봉(bar)이 아예 없는 날(주말, 휴일)에 매 호출마다 다운로드가
# 일어나지 않을 만큼 긴 값.
OHLCV_CACHE_TTL_SECONDS = 900


def yf_retry(func, max_retries=3, base_delay=2.0):
    """요청 제한(rate limit)에 지수 백오프(exponential backoff)로 재시도하며 yfinance 호출을 실행한다.

    yfinance는 HTTP 429 응답에 YFRateLimitError를 던지지만 내부적으로
    재시도하지는 않습니다. 이 래퍼(wrapper)는 요청 제한에 한정된 재시도
    로직을 더합니다. 다른 예외는 즉시 전파됩니다.
    """
    for attempt in range(max_retries + 1):
        try:
            return func()
        except YFRateLimitError:
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                logger.warning(f"Yahoo Finance rate limited, retrying in {delay:.0f}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(delay)
            else:
                raise


def _ensure_date_column(data: pd.DataFrame) -> pd.DataFrame:
    """날짜 컬럼 이름을 ``Date``로 정규화한다.

    일부 yfinance 빌드는 인덱스에 이름을 붙이지 않거나(``reset_index()``가
    ``index``를 만들어냄) 장중 데이터에 ``Datetime``을 씁니다. 컬럼 이름이
    ``Date``가 아닐 때 지표 계산이 조용히 누락되지 않도록, 첫 번째
    날짜성 컬럼의 이름을 바꿔 줍니다.
    """
    if "Date" in data.columns:
        return data
    for candidate in ("index", "Datetime", "date"):
        if candidate in data.columns:
            return data.rename(columns={candidate: "Date"})
    return data


def _clean_dataframe(data: pd.DataFrame) -> pd.DataFrame:
    """주가 DataFrame을 stockstats용으로 정규화한다: 날짜 파싱, 잘못된 행 제거, 가격 결측 채움."""
    data = _ensure_date_column(data)
    data["Date"] = pd.to_datetime(data["Date"], errors="coerce")
    data = data.dropna(subset=["Date"])

    price_cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in data.columns]
    data[price_cols] = data[price_cols].apply(pd.to_numeric, errors="coerce")
    data = data.dropna(subset=["Close"])
    data[price_cols] = data[price_cols].ffill().bfill()

    return data


def _coerce_ohlcv_dates(data: pd.DataFrame) -> pd.Series:
    """OHLCV 프레임에서 Date가 컬럼이든 인덱스든 상관없이 파싱된 날짜들을 반환한다."""
    if "Date" in data.columns:
        return pd.to_datetime(data["Date"], errors="coerce").dropna()
    # yfinance는 날짜를 인덱스(DatetimeIndex, 때로는 이름 없음)에 담아 둔다.
    if isinstance(data.index, pd.DatetimeIndex):
        return pd.Series(pd.to_datetime(data.index, errors="coerce")).dropna()
    # 폴백: 인덱스를 컬럼으로 꺼낸 뒤 날짜성 컬럼을 찾는다.
    df = data.reset_index()
    for col in ("Date", "Datetime", "date", "index"):
        if col in df.columns:
            parsed = pd.to_datetime(df[col], errors="coerce").dropna()
            if not parsed.empty:
                return parsed
    return pd.Series(dtype="datetime64[ns]")


def _assert_ohlcv_not_stale(
    data: pd.DataFrame,
    curr_date: str,
    symbol: str,
    canonical: str | None = None,
    *,
    max_stale_days: int = MAX_OHLCV_STALE_DAYS,
) -> None:
    """최신 행이 curr_date보다 지나치게 오래된 OHLCV를 거부한다.

    NoMarketDataError를(진부함을 명시한 상세 메시지와 함께) 던져서, 라우터가
    이를 다른 "이 벤더에서 쓸 만한 데이터 없음"과 똑같이 처리하게 합니다 —
    다음 벤더를 시도한 뒤 명확한 '이용 불가' 신호 하나를 내보냅니다. 빈
    프레임은 호출자의 기존 데이터-없음 처리에 맡깁니다; 이 함수는 오직
    '있긴 하지만 진부한 행'이라는 위험한 경우(벤더가 1년 묵은 프레임을
    돌려줘 에이전트에 잘못된 가격이 흘러가는 경우, #1021)만 막습니다.
    """
    if data is None or data.empty:
        return
    requested = pd.to_datetime(curr_date, errors="coerce")
    if pd.isna(requested):
        return
    requested = requested.normalize()
    dates = _coerce_ohlcv_dates(data)
    if dates.empty:
        return
    latest = dates.max().normalize()
    stale_days = (requested - latest).days
    if stale_days > max_stale_days:
        raise NoMarketDataError(
            symbol,
            canonical,
            f"latest row is {latest.date()}, {stale_days} days before the "
            f"requested {requested.date()} (stale) — refusing to use it",
        )


def _needs_same_day_refresh(data_file, curr_date_dt, today_date) -> bool:
    """캐시된 프레임을 요청한 날짜에 맞게 다시 받아야 하는지 여부.

    캐시 파일은 하루 단위로 키가 잡히므로, 이 검사가 없으면 당일 봉(bar)이
    확정되기 전에 시작된 실행이 그 스냅샷을 이후의 모든 실행에 계속 제공하게
    됩니다(#1150). 당일 요청에는 두 가지 서로 다른 진부함이 존재합니다:
    봉이 아예 없거나, 있지만 아직 진행 중이거나 — 야후는 장중에 부분(partial)
    일봉을 게시하는데, 그 ``Close``는 종가가 아닙니다. 행을 들여다보는 것만으로는
    부분 봉과 확정 봉을 구별할 수 없으므로 당일 캐시는 모두 TTL(유효 기간)이
    관리합니다. 과거 날짜 요청은 항상 캐시를 재사용합니다 — 그 행들은
    불변(immutable)이기 때문입니다.
    """
    if curr_date_dt.date() < today_date.date():
        return False
    return time.time() - os.path.getmtime(data_file) > OHLCV_CACHE_TTL_SECONDS


def load_ohlcv(symbol: str, curr_date: str) -> pd.DataFrame:
    """OHLCV 데이터를 캐시와 함께 가져오되, 선견 편향(look-ahead bias)을 막도록 필터링한다.

    오늘까지의 5년치 데이터를 내려받아 심볼별로 캐시합니다. 이후 호출은
    캐시를 재사용합니다. curr_date 이후의 행은 걸러내어 백테스트가 미래
    가격을 절대 보지 못하게 합니다.
    """
    # 브로커/외환 심볼(XAUUSD+ -> GC=F)을 야후 규칙으로 변환한 뒤,
    # 캐시 파일명에 끼워 넣었을 때 캐시 디렉터리를 벗어날 수 있는 값
    # (예: ``../../tmp/x``)을 거부합니다.
    canonical = normalize_symbol(symbol)
    safe_symbol = safe_ticker_component(canonical)

    config = get_config()
    curr_date_dt = pd.to_datetime(curr_date)

    # 캐시는 고정 구간(5년 전 ~ 오늘)을 쓰므로 심볼당 파일이 하나다.
    today_date = pd.Timestamp.today()
    start_date = today_date - pd.DateOffset(years=5)
    start_str = start_date.strftime("%Y-%m-%d")
    # yfinance의 ``end``는 미포함(EXCLUSIVE)이므로, curr_date가 오늘일 때
    # 오늘 행이 포함되도록 내일을 요청합니다(#986). 선견 편향은 아래의
    # curr_date 필터가 여전히 막아 줍니다.
    end_str = (today_date + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    os.makedirs(config["data_cache_dir"], exist_ok=True)
    data_file = os.path.join(
        config["data_cache_dir"],
        f"{safe_symbol}-YFin-data-{start_str}-{end_str}.csv",
    )

    # 이전 가져오기가 실패했다면(알 수 없는 심볼, 일시적 요청 제한) 캐시
    # 파일이 비어 있을 수 있습니다. 비어 있거나 컬럼이 없는 캐시는 캐시
    # 미스(miss)로 취급하고 다시 가져옵니다 — 오염된 파일을 영원히 제공하지 않도록.
    data = None
    if os.path.exists(data_file):
        cached = pd.read_csv(data_file, on_bad_lines="skip", encoding="utf-8")
        # 캐시가 쓸 만하고, 요청한 당일의 진부한 스냅샷(#1150)이 아닐 때만
        # 캐시를 제공합니다; 아니면 아래로 내려가 다시 받아 옵니다.
        if (
            not cached.empty
            and "Close" in cached.columns
            and not _needs_same_day_refresh(data_file, curr_date_dt, today_date)
        ):
            data = cached

    if data is None:
        downloaded = yf_retry(lambda: yf.download(
            canonical,
            start=start_str,
            end=end_str,
            multi_level_index=False,
            progress=False,
            auto_adjust=True,
        ))
        downloaded = _ensure_date_column(downloaded.reset_index())
        # 진짜 데이터만 캐시한다 — 빈 프레임은 절대 저장하지 않는다.
        if downloaded.empty or "Close" not in downloaded.columns:
            raise NoMarketDataError(
                symbol, canonical, "Yahoo Finance returned no rows"
            )
        downloaded.to_csv(data_file, index=False, encoding="utf-8")
        data = downloaded

    data = _clean_dataframe(data)

    # 백테스팅의 선견 편향을 막기 위해 curr_date까지로 필터링한다
    data = data[data["Date"] <= curr_date_dt]

    # 진부한 프레임(최신 행이 curr_date보다 훨씬 오래됨)은 1년 묵은 가격을
    # 지표에 흘려 넣는 대신 거부한다(#1021).
    _assert_ohlcv_not_stale(data, curr_date, symbol, canonical)

    return data


def filter_financials_by_date(data: pd.DataFrame, curr_date: str) -> pd.DataFrame:
    """curr_date 이후의 재무제표 컬럼(회계 기간 타임스탬프)을 제거한다.

    yfinance 재무제표는 회계 기간 종료일을 컬럼으로 씁니다. curr_date
    이후의 컬럼은 미래 데이터를 나타내므로 선견 편향(look-ahead bias)을
    막기 위해 제거합니다.
    """
    if not curr_date or data.empty:
        return data
    cutoff = pd.Timestamp(curr_date)
    mask = pd.to_datetime(data.columns, errors="coerce") <= cutoff
    return data.loc[:, mask]


class StockstatsUtils:
    @staticmethod
    def get_stock_stats(
        symbol: Annotated[str, "ticker symbol for the company"],
        indicator: Annotated[
            str, "quantitative indicators based off of the stock data for the company"
        ],
        curr_date: Annotated[
            str, "curr date for retrieving stock price data, YYYY-mm-dd"
        ],
    ):
        data = load_ohlcv(symbol, curr_date)
        df = wrap(data)
        df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")
        curr_date_str = pd.to_datetime(curr_date).strftime("%Y-%m-%d")

        df[indicator]  # stockstats가 이 지표를 계산하도록 트리거한다
        matching_rows = df[df["Date"].str.startswith(curr_date_str)]

        if not matching_rows.empty:
            indicator_value = matching_rows[indicator].values[0]
            return indicator_value
        else:
            return "N/A: Not a trading day (weekend or holiday)"
