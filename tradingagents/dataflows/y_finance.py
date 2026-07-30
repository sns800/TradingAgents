# ============================================================================
# [모듈 개요 - 초보자용]
# 이 파일은 야후 파이낸스(Yahoo Finance)에서 주가(OHLCV: 시가·고가·저가·종가·
# 거래량), 기술적 지표, 기업 재무제표(재무상태표·현금흐름표·손익계산서),
# 내부자 거래 데이터를 가져오는 벤더 구현 모듈입니다.
# TradingAgents(LLM 멀티 에이전트 주식 트레이딩 프레임워크)에서 벤더 라우팅
# 계층(interface.py)이 "yfinance" 벤더로 설정됐을 때 호출하는 실제 함수들이
# 모두 여기에 있습니다.
# ============================================================================

from datetime import datetime
from typing import Annotated

import pandas as pd
import yfinance as yf
from dateutil.relativedelta import relativedelta

from .stockstats_utils import (
    StockstatsUtils,
    _assert_ohlcv_not_stale,
    filter_financials_by_date,
    load_ohlcv,
    yf_retry,
)
from .symbol_utils import NoMarketDataError, normalize_symbol


def get_YFin_data_online(
    symbol: Annotated[str, "ticker symbol of the company"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
):
    # 지정한 기간의 OHLCV 주가 데이터를 야후 파이낸스에서 받아 CSV 문자열로 반환한다.

    datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    # 브로커/외환 심볼을 야후 규칙으로 변환한다 (XAUUSD+ -> GC=F).
    canonical = normalize_symbol(symbol)
    ticker = yf.Ticker(canonical)

    # yfinance는 ``end``를 미포함(EXCLUSIVE)으로 취급하므로 요청한 end_date
    # 행(그리고 end_date가 오늘이면 당일)이 빠지게 됩니다. end_date보다
    # 하루 뒤를 요청해서 요청 범위가 실제로 포함되게 합니다(#986/#987).
    end_inclusive = (end_dt + relativedelta(days=1)).strftime("%Y-%m-%d")
    data = yf_retry(lambda: ticker.history(start=start_date, end=end_inclusive))

    # 빈 결과는 심볼이 알 수 없거나 상장폐지됐다는 뜻입니다. 산문을 반환하는
    # 대신 타입 있는 오류를 던집니다: 라우팅 계층이 이를 단 하나의 명확한
    # "데이터 없음" 신호로 바꿔 에이전트가 가격을 지어내지 못하게 합니다.
    if data.empty:
        raise NoMarketDataError(
            symbol, canonical, f"no rows between {start_date} and {end_date}"
        )

    # 더 깔끔한 출력을 위해 인덱스에서 시간대(timezone) 정보를 제거한다
    if data.index.tz is not None:
        data.index = data.index.tz_localize(None)

    # 진부한(stale) 프레임(예: 1년 묵은 부분 응답)은 보고서에 포매팅되기
    # 전에 거부합니다. NoMarketDataError를 던지면 라우터가 명확한
    # '이용 불가' 신호 하나로 바꿉니다(#1021).
    _assert_ohlcv_not_stale(data, end_date, symbol, canonical)

    # 더 깔끔한 표시를 위해 숫자 값을 소수점 둘째 자리로 반올림한다
    numeric_columns = ["Open", "High", "Low", "Close", "Adj Close"]
    for col in numeric_columns:
        if col in data.columns:
            data[col] = data[col].round(2)

    # DataFrame을 CSV 문자열로 변환한다
    csv_string = data.to_csv()

    # 헤더 정보를 추가한다; 변환된 심볼이 원본과 다르면 함께 적어서
    # 에이전트(와 사용자)가 실제로 어떤 종목의 가격인지 볼 수 있게 한다.
    label = canonical if canonical == symbol.upper() else f"{canonical} (from {symbol})"
    header = f"# Stock data for {label} from {start_date} to {end_date}\n"
    header += f"# Total records: {len(data)}\n"
    header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"

    return header + csv_string

def get_stock_stats_indicators_window(
    symbol: Annotated[str, "ticker symbol of the company"],
    indicator: Annotated[str, "technical indicator to get the analysis and report of"],
    curr_date: Annotated[
        str, "The current trading date you are trading on, YYYY-mm-dd"
    ],
    look_back_days: Annotated[int, "how many days to look back"],
) -> str:
    # 지정한 기술적 지표를 과거 look_back_days일 동안 날짜별로 계산해
    # 보고서 문자열을 만든다. 아래 딕셔너리는 각 지표의 설명문
    # (에이전트 프롬프트에 그대로 들어가므로 영어 유지).

    best_ind_params = {
        # 이동평균(Moving Averages)
        "close_50_sma": (
            "50 SMA: A medium-term trend indicator. "
            "Usage: Identify trend direction and serve as dynamic support/resistance. "
            "Tips: It lags price; combine with faster indicators for timely signals."
        ),
        "close_200_sma": (
            "200 SMA: A long-term trend benchmark. "
            "Usage: Confirm overall market trend and identify golden/death cross setups. "
            "Tips: It reacts slowly; best for strategic trend confirmation rather than frequent trading entries."
        ),
        "close_10_ema": (
            "10 EMA: A responsive short-term average. "
            "Usage: Capture quick shifts in momentum and potential entry points. "
            "Tips: Prone to noise in choppy markets; use alongside longer averages for filtering false signals."
        ),
        # MACD(이동평균수렴확산) 관련
        "macd": (
            "MACD: Computes momentum via differences of EMAs. "
            "Usage: Look for crossovers and divergence as signals of trend changes. "
            "Tips: Confirm with other indicators in low-volatility or sideways markets."
        ),
        "macds": (
            "MACD Signal: An EMA smoothing of the MACD line. "
            "Usage: Use crossovers with the MACD line to trigger trades. "
            "Tips: Should be part of a broader strategy to avoid false positives."
        ),
        "macdh": (
            "MACD Histogram: Shows the gap between the MACD line and its signal. "
            "Usage: Visualize momentum strength and spot divergence early. "
            "Tips: Can be volatile; complement with additional filters in fast-moving markets."
        ),
        # 모멘텀(momentum) 지표
        "rsi": (
            "RSI: Measures momentum to flag overbought/oversold conditions. "
            "Usage: Apply 70/30 thresholds and watch for divergence to signal reversals. "
            "Tips: In strong trends, RSI may remain extreme; always cross-check with trend analysis."
        ),
        # 변동성(volatility) 지표
        "boll": (
            "Bollinger Middle: A 20 SMA serving as the basis for Bollinger Bands. "
            "Usage: Acts as a dynamic benchmark for price movement. "
            "Tips: Combine with the upper and lower bands to effectively spot breakouts or reversals."
        ),
        "boll_ub": (
            "Bollinger Upper Band: Typically 2 standard deviations above the middle line. "
            "Usage: Signals potential overbought conditions and breakout zones. "
            "Tips: Confirm signals with other tools; prices may ride the band in strong trends."
        ),
        "boll_lb": (
            "Bollinger Lower Band: Typically 2 standard deviations below the middle line. "
            "Usage: Indicates potential oversold conditions. "
            "Tips: Use additional analysis to avoid false reversal signals."
        ),
        "atr": (
            "ATR: Averages true range to measure volatility. "
            "Usage: Set stop-loss levels and adjust position sizes based on current market volatility. "
            "Tips: It's a reactive measure, so use it as part of a broader risk management strategy."
        ),
        # 거래량 기반 지표
        "vwma": (
            "VWMA: A moving average weighted by volume. "
            "Usage: Confirm trends by integrating price action with volume data. "
            "Tips: Watch for skewed results from volume spikes; use in combination with other volume analyses."
        ),
        "mfi": (
            "MFI: The Money Flow Index is a momentum indicator that uses both price and volume to measure buying and selling pressure. "
            "Usage: Identify overbought (>80) or oversold (<20) conditions and confirm the strength of trends or reversals. "
            "Tips: Use alongside RSI or MACD to confirm signals; divergence between price and MFI can indicate potential reversals."
        ),
    }

    if indicator not in best_ind_params:
        raise ValueError(
            f"Indicator {indicator} is not supported. Please choose from: {list(best_ind_params.keys())}"
        )

    end_date = curr_date
    curr_date_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    before = curr_date_dt - relativedelta(days=look_back_days)

    # 최적화: 주가 데이터를 한 번만 가져와 모든 날짜의 지표를 계산한다
    try:
        indicator_data = _get_stock_stats_bulk(symbol, indicator, curr_date)

        # 필요한 날짜 범위를 생성한다
        current_dt = curr_date_dt
        date_values = []

        while current_dt >= before:
            date_str = current_dt.strftime('%Y-%m-%d')

            # 이 날짜의 지표 값을 조회한다
            if date_str in indicator_data:
                indicator_value = indicator_data[date_str]
            else:
                indicator_value = "N/A: Not a trading day (weekend or holiday)"

            date_values.append((date_str, indicator_value))
            current_dt = current_dt - relativedelta(days=1)

        # 결과 문자열을 조립한다
        ind_string = ""
        for date_str, value in date_values:
            ind_string += f"{date_str}: {value}\n"

    except NoMarketDataError:
        raise  # 알 수 없거나 상장폐지된 심볼 — 라우터가 센티널을 내보내게 둔다
    except Exception as e:
        print(f"Error getting bulk stockstats data: {e}")
        # 일괄(bulk) 방식이 실패하면 원래 구현으로 폴백한다
        ind_string = ""
        curr_date_dt = datetime.strptime(curr_date, "%Y-%m-%d")
        while curr_date_dt >= before:
            indicator_value = get_stockstats_indicator(
                symbol, indicator, curr_date_dt.strftime("%Y-%m-%d")
            )
            ind_string += f"{curr_date_dt.strftime('%Y-%m-%d')}: {indicator_value}\n"
            curr_date_dt = curr_date_dt - relativedelta(days=1)

    result_str = (
        f"## {indicator} values from {before.strftime('%Y-%m-%d')} to {end_date}:\n\n"
        + ind_string
        + "\n\n"
        + best_ind_params.get(indicator, "No description available.")
    )

    return result_str


def _get_stock_stats_bulk(
    symbol: Annotated[str, "ticker symbol of the company"],
    indicator: Annotated[str, "technical indicator to calculate"],
    curr_date: Annotated[str, "current date for reference"]
) -> dict:
    """
    주가 지표를 최적화된 방식으로 일괄 계산한다.
    데이터를 한 번만 가져와 사용 가능한 모든 날짜에 대해 지표를 계산한다.
    날짜 문자열을 지표 값에 매핑하는 dict를 반환한다.
    """
    from stockstats import wrap

    data = load_ohlcv(symbol, curr_date)
    df = wrap(data)
    df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")

    # 모든 행의 지표를 한 번에 계산한다
    df[indicator]  # 이 접근이 stockstats의 지표 계산을 트리거한다

    # 날짜 문자열 -> 지표 값 딕셔너리를 만든다
    result_dict = {}
    for _, row in df.iterrows():
        date_str = row["Date"]
        indicator_value = row[indicator]

        # NaN/None 값 처리
        if pd.isna(indicator_value):
            result_dict[date_str] = "N/A"
        else:
            result_dict[date_str] = str(indicator_value)

    return result_dict


def get_stockstats_indicator(
    symbol: Annotated[str, "ticker symbol of the company"],
    indicator: Annotated[str, "technical indicator to get the analysis and report of"],
    curr_date: Annotated[
        str, "The current trading date you are trading on, YYYY-mm-dd"
    ],
) -> str:
    # 특정 날짜 하루의 지표 값을 문자열로 반환한다(폴백 경로에서 사용).

    curr_date_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    curr_date = curr_date_dt.strftime("%Y-%m-%d")

    try:
        indicator_value = StockstatsUtils.get_stock_stats(
            symbol,
            indicator,
            curr_date,
        )
    except NoMarketDataError:
        raise  # 알 수 없거나 상장폐지된 심볼 — 라우터가 센티널을 내보내게 둔다
    except Exception as e:
        print(
            f"Error getting stockstats indicator data for indicator {indicator} on {curr_date}: {e}"
        )
        return ""

    return str(indicator_value)


def get_fundamentals(
    ticker: Annotated[str, "ticker symbol of the company"],
    curr_date: Annotated[str, "current date (not used for yfinance)"] = None
):
    """yfinance에서 기업 기초 정보(fundamentals) 개요를 가져온다."""
    canonical = normalize_symbol(ticker)
    try:
        ticker_obj = yf.Ticker(canonical)
        info = yf_retry(lambda: ticker_obj.info)

        if not info:
            raise NoMarketDataError(ticker, canonical, "no fundamentals returned")

        # 표시할 재무 지표 필드 목록: PER(주가수익비율), EPS(주당순이익),
        # 배당수익률, 베타(시장 대비 변동성) 등.
        fields = [
            ("Name", info.get("longName")),
            ("Sector", info.get("sector")),
            ("Industry", info.get("industry")),
            ("Market Cap", info.get("marketCap")),
            ("PE Ratio (TTM)", info.get("trailingPE")),
            ("Forward PE", info.get("forwardPE")),
            ("PEG Ratio", info.get("pegRatio")),
            ("Price to Book", info.get("priceToBook")),
            ("EPS (TTM)", info.get("trailingEps")),
            ("Forward EPS", info.get("forwardEps")),
            ("Dividend Yield", info.get("dividendYield")),
            ("Beta", info.get("beta")),
            ("52 Week High", info.get("fiftyTwoWeekHigh")),
            ("52 Week Low", info.get("fiftyTwoWeekLow")),
            ("50 Day Average", info.get("fiftyDayAverage")),
            ("200 Day Average", info.get("twoHundredDayAverage")),
            ("Revenue (TTM)", info.get("totalRevenue")),
            ("Gross Profit", info.get("grossProfits")),
            ("EBITDA", info.get("ebitda")),
            ("Net Income", info.get("netIncomeToCommon")),
            ("Profit Margin", info.get("profitMargins")),
            ("Operating Margin", info.get("operatingMargins")),
            ("Return on Equity", info.get("returnOnEquity")),
            ("Return on Assets", info.get("returnOnAssets")),
            ("Debt to Equity", info.get("debtToEquity")),
            ("Current Ratio", info.get("currentRatio")),
            ("Book Value", info.get("bookValue")),
            ("Free Cash Flow", info.get("freeCashflow")),
        ]

        lines = []
        for label, value in fields:
            if value is not None:
                lines.append(f"{label}: {value}")

        # yfinance는 알 수 없는 심볼에 대해 스텁(stub) 딕셔너리(예:
        # {"trailingPegRatio": None})를 반환하므로, `info`가 참(truthy)이어도
        # 모든 필드가 비어 있을 수 있습니다. "쓸 만한 필드 없음"을 데이터
        # 없음으로 취급하여, 에이전트가 그 주변에서 지어낼 수 있는 빈 헤더를
        # 내보내지 않습니다.
        if not lines:
            raise NoMarketDataError(ticker, canonical, "no fundamental fields returned")

        header = f"# Company Fundamentals for {canonical}\n"
        header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"

        return header + "\n".join(lines)

    except NoMarketDataError:
        raise
    except Exception as e:
        return f"Error retrieving fundamentals for {ticker}: {str(e)}"


def get_balance_sheet(
    ticker: Annotated[str, "ticker symbol of the company"],
    freq: Annotated[str, "frequency of data: 'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str, "current date in YYYY-MM-DD format"] = None
):
    """yfinance에서 재무상태표(balance sheet) 데이터를 가져온다."""
    canonical = normalize_symbol(ticker)
    try:
        ticker_obj = yf.Ticker(canonical)

        if freq.lower() == "quarterly":
            data = yf_retry(lambda: ticker_obj.quarterly_balance_sheet)
        else:
            data = yf_retry(lambda: ticker_obj.balance_sheet)

        data = filter_financials_by_date(data, curr_date)

        if data.empty:
            raise NoMarketDataError(ticker, canonical, "no balance sheet data")

        # 다른 함수들과 일관되게 CSV 문자열로 변환한다
        csv_string = data.to_csv()

        # 헤더 정보를 추가한다
        header = f"# Balance Sheet data for {canonical} ({freq})\n"
        header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"

        return header + csv_string

    except NoMarketDataError:
        raise
    except Exception as e:
        return f"Error retrieving balance sheet for {ticker}: {str(e)}"


def get_cashflow(
    ticker: Annotated[str, "ticker symbol of the company"],
    freq: Annotated[str, "frequency of data: 'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str, "current date in YYYY-MM-DD format"] = None
):
    """yfinance에서 현금흐름표(cash flow) 데이터를 가져온다."""
    canonical = normalize_symbol(ticker)
    try:
        ticker_obj = yf.Ticker(canonical)

        if freq.lower() == "quarterly":
            data = yf_retry(lambda: ticker_obj.quarterly_cashflow)
        else:
            data = yf_retry(lambda: ticker_obj.cashflow)

        data = filter_financials_by_date(data, curr_date)

        if data.empty:
            raise NoMarketDataError(ticker, canonical, "no cash flow data")

        # 다른 함수들과 일관되게 CSV 문자열로 변환한다
        csv_string = data.to_csv()

        # 헤더 정보를 추가한다
        header = f"# Cash Flow data for {canonical} ({freq})\n"
        header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"

        return header + csv_string

    except NoMarketDataError:
        raise
    except Exception as e:
        return f"Error retrieving cash flow for {ticker}: {str(e)}"


def get_income_statement(
    ticker: Annotated[str, "ticker symbol of the company"],
    freq: Annotated[str, "frequency of data: 'annual' or 'quarterly'"] = "quarterly",
    curr_date: Annotated[str, "current date in YYYY-MM-DD format"] = None
):
    """yfinance에서 손익계산서(income statement) 데이터를 가져온다."""
    canonical = normalize_symbol(ticker)
    try:
        ticker_obj = yf.Ticker(canonical)

        if freq.lower() == "quarterly":
            data = yf_retry(lambda: ticker_obj.quarterly_income_stmt)
        else:
            data = yf_retry(lambda: ticker_obj.income_stmt)

        data = filter_financials_by_date(data, curr_date)

        if data.empty:
            raise NoMarketDataError(ticker, canonical, "no income statement data")

        # 다른 함수들과 일관되게 CSV 문자열로 변환한다
        csv_string = data.to_csv()

        # 헤더 정보를 추가한다
        header = f"# Income Statement data for {canonical} ({freq})\n"
        header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"

        return header + csv_string

    except NoMarketDataError:
        raise
    except Exception as e:
        return f"Error retrieving income statement for {ticker}: {str(e)}"


def get_insider_transactions(
    ticker: Annotated[str, "ticker symbol of the company"]
):
    """yfinance에서 내부자 거래(insider transactions) 데이터를 가져온다."""
    canonical = normalize_symbol(ticker)
    try:
        ticker_obj = yf.Ticker(canonical)
        data = yf_retry(lambda: ticker_obj.insider_transactions)

        # 여기서 빈 결과는 정상입니다(내부자 신고가 없는 유효한 심볼이 많음).
        # 심볼이 잘못됐다고 취급하는 대신 그대로 알려 줍니다.
        if data is None or data.empty:
            return f"No insider transactions reported for symbol '{canonical}'"

        # 다른 함수들과 일관되게 CSV 문자열로 변환한다
        csv_string = data.to_csv()

        # 헤더 정보를 추가한다
        header = f"# Insider Transactions data for {canonical}\n"
        header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"

        return header + csv_string

    except Exception as e:
        return f"Error retrieving insider transactions for {ticker}: {str(e)}"
