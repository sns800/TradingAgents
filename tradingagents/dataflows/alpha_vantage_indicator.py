# =============================================================================
# [모듈 개요 - 초보자용 안내]
# 이 파일은 Alpha Vantage API에서 기술적 지표(technical indicator) 데이터를 가져오는
# 모듈입니다. 이동평균(SMA/EMA), MACD, RSI, 볼린저 밴드(Bollinger Bands), ATR 등
# 차트 분석에 쓰이는 지표 값을 조회하고 사람이 읽기 좋은 문자열로 정리합니다.
# TradingAgents(LLM 멀티 에이전트 주식 트레이딩 프레임워크)에서 마켓(기술적 분석)
# 애널리스트 에이전트가 추세·모멘텀·변동성을 판단할 때 이 모듈을 사용합니다.
# =============================================================================
from .alpha_vantage_common import AlphaVantageNotConfiguredError, _make_api_request


def get_indicator(
    symbol: str,
    indicator: str,
    curr_date: str,
    look_back_days: int,
    interval: str = "daily",
    time_period: int = 14,
    series_type: str = "close"
) -> str:
    """
    지정한 기간(time window)에 대한 Alpha Vantage 기술적 지표 값을 반환한다.

    Args:
        symbol: 회사의 티커 심볼(ticker symbol)
        indicator: 분석 및 리포트를 받을 기술적 지표 이름
        curr_date: 현재 트레이딩 중인 날짜, YYYY-mm-dd 형식
        look_back_days: 과거 며칠까지 조회할지(룩백 일수)
        interval: 시간 간격 (daily, weekly, monthly)
        time_period: 지표 계산에 사용할 데이터 포인트 개수
        series_type: 사용할 가격 종류 (close, open, high, low)

    Returns:
        지표 값과 설명이 담긴 문자열
    """
    from datetime import datetime

    from dateutil.relativedelta import relativedelta

    supported_indicators = {
        "close_50_sma": ("50 SMA", "close"),
        "close_200_sma": ("200 SMA", "close"),
        "close_10_ema": ("10 EMA", "close"),
        "macd": ("MACD", "close"),
        "macds": ("MACD Signal", "close"),
        "macdh": ("MACD Histogram", "close"),
        "rsi": ("RSI", "close"),
        "boll": ("Bollinger Middle", "close"),
        "boll_ub": ("Bollinger Upper Band", "close"),
        "boll_lb": ("Bollinger Lower Band", "close"),
        "atr": ("ATR", None),
        "vwma": ("VWMA", "close")
    }

    # 지표별 설명 문구. LLM 에이전트가 읽는 리포트 본문에 그대로 포함되므로
    # 영어 원문을 유지한다.
    indicator_descriptions = {
        "close_50_sma": "50 SMA: A medium-term trend indicator. Usage: Identify trend direction and serve as dynamic support/resistance. Tips: It lags price; combine with faster indicators for timely signals.",
        "close_200_sma": "200 SMA: A long-term trend benchmark. Usage: Confirm overall market trend and identify golden/death cross setups. Tips: It reacts slowly; best for strategic trend confirmation rather than frequent trading entries.",
        "close_10_ema": "10 EMA: A responsive short-term average. Usage: Capture quick shifts in momentum and potential entry points. Tips: Prone to noise in choppy markets; use alongside longer averages for filtering false signals.",
        "macd": "MACD: Computes momentum via differences of EMAs. Usage: Look for crossovers and divergence as signals of trend changes. Tips: Confirm with other indicators in low-volatility or sideways markets.",
        "macds": "MACD Signal: An EMA smoothing of the MACD line. Usage: Use crossovers with the MACD line to trigger trades. Tips: Should be part of a broader strategy to avoid false positives.",
        "macdh": "MACD Histogram: Shows the gap between the MACD line and its signal. Usage: Visualize momentum strength and spot divergence early. Tips: Can be volatile; complement with additional filters in fast-moving markets.",
        "rsi": "RSI: Measures momentum to flag overbought/oversold conditions. Usage: Apply 70/30 thresholds and watch for divergence to signal reversals. Tips: In strong trends, RSI may remain extreme; always cross-check with trend analysis.",
        "boll": "Bollinger Middle: A 20 SMA serving as the basis for Bollinger Bands. Usage: Acts as a dynamic benchmark for price movement. Tips: Combine with the upper and lower bands to effectively spot breakouts or reversals.",
        "boll_ub": "Bollinger Upper Band: Typically 2 standard deviations above the middle line. Usage: Signals potential overbought conditions and breakout zones. Tips: Confirm signals with other tools; prices may ride the band in strong trends.",
        "boll_lb": "Bollinger Lower Band: Typically 2 standard deviations below the middle line. Usage: Indicates potential oversold conditions. Tips: Use additional analysis to avoid false reversal signals.",
        "atr": "ATR: Averages true range to measure volatility. Usage: Set stop-loss levels and adjust position sizes based on current market volatility. Tips: It's a reactive measure, so use it as part of a broader risk management strategy.",
        "vwma": "VWMA: A moving average weighted by volume. Usage: Confirm trends by integrating price action with volume data. Tips: Watch for skewed results from volume spikes; use in combination with other volume analyses."
    }

    if indicator not in supported_indicators:
        raise ValueError(
            f"Indicator {indicator} is not supported. Please choose from: {list(supported_indicators.keys())}"
        )

    curr_date_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    before = curr_date_dt - relativedelta(days=look_back_days)

    # 날짜별로 개별 호출을 반복하는 대신 기간 전체 데이터를 한 번에 가져온다
    _, required_series_type = supported_indicators[indicator]

    # 지표가 특정 가격 종류를 요구하면 그것을 사용하고, 아니면 인자로 받은 값을 사용
    if required_series_type:
        series_type = required_series_type

    try:
        # 해당 기간의 지표 데이터를 가져온다
        if indicator == "close_50_sma":
            data = _make_api_request("SMA", {
                "symbol": symbol,
                "interval": interval,
                "time_period": "50",
                "series_type": series_type,
                "datatype": "csv"
            })
        elif indicator == "close_200_sma":
            data = _make_api_request("SMA", {
                "symbol": symbol,
                "interval": interval,
                "time_period": "200",
                "series_type": series_type,
                "datatype": "csv"
            })
        elif indicator == "close_10_ema":
            data = _make_api_request("EMA", {
                "symbol": symbol,
                "interval": interval,
                "time_period": "10",
                "series_type": series_type,
                "datatype": "csv"
            })
        elif indicator == "macd" or indicator == "macds" or indicator == "macdh":
            data = _make_api_request("MACD", {
                "symbol": symbol,
                "interval": interval,
                "series_type": series_type,
                "datatype": "csv"
            })
        elif indicator == "rsi":
            data = _make_api_request("RSI", {
                "symbol": symbol,
                "interval": interval,
                "time_period": str(time_period),
                "series_type": series_type,
                "datatype": "csv"
            })
        elif indicator in ["boll", "boll_ub", "boll_lb"]:
            data = _make_api_request("BBANDS", {
                "symbol": symbol,
                "interval": interval,
                "time_period": "20",
                "series_type": series_type,
                "datatype": "csv"
            })
        elif indicator == "atr":
            data = _make_api_request("ATR", {
                "symbol": symbol,
                "interval": interval,
                "time_period": str(time_period),
                "datatype": "csv"
            })
        elif indicator == "vwma":
            # Alpha Vantage에는 VWMA가 직접 제공되지 않으므로 안내 메시지를 반환한다.
            # 실제로 구현하려면 OHLCV(시가·고가·저가·종가·거래량) 데이터로 직접 계산해야 한다.
            return f"## VWMA (Volume Weighted Moving Average) for {symbol}:\n\nVWMA calculation requires OHLCV data and is not directly available from Alpha Vantage API.\nThis indicator would need to be calculated from the raw stock data using volume-weighted price averaging.\n\n{indicator_descriptions.get('vwma', 'No description available.')}"
        else:
            return f"Error: Indicator {indicator} not implemented yet."

        # CSV 데이터를 파싱해 원하는 날짜 범위의 값을 추출한다
        lines = data.strip().split('\n')
        if len(lines) < 2:
            return f"Error: No data returned for {indicator}"

        # 헤더와 데이터 파싱
        header = [col.strip() for col in lines[0].split(',')]
        try:
            date_col_idx = header.index('time')
        except ValueError:
            return f"Error: 'time' column not found in data for {indicator}. Available columns: {header}"

        # 내부에서 쓰는 지표 이름을 Alpha Vantage CSV 열(column) 이름으로 매핑
        col_name_map = {
            "macd": "MACD", "macds": "MACD_Signal", "macdh": "MACD_Hist",
            "boll": "Real Middle Band", "boll_ub": "Real Upper Band", "boll_lb": "Real Lower Band",
            "rsi": "RSI", "atr": "ATR", "close_10_ema": "EMA",
            "close_50_sma": "SMA", "close_200_sma": "SMA"
        }

        target_col_name = col_name_map.get(indicator)

        if not target_col_name:
            # 매핑이 없으면 기본적으로 두 번째 열을 사용한다
            value_col_idx = 1
        else:
            try:
                value_col_idx = header.index(target_col_name)
            except ValueError:
                return f"Error: Column '{target_col_name}' not found for indicator '{indicator}'. Available columns: {header}"

        result_data = []
        for line in lines[1:]:
            if not line.strip():
                continue
            values = line.split(',')
            if len(values) > value_col_idx:
                try:
                    date_str = values[date_col_idx].strip()
                    # 날짜 파싱
                    date_dt = datetime.strptime(date_str, "%Y-%m-%d")

                    # 우리가 원하는 날짜 범위에 포함되는지 확인.
                    # curr_date 이후(미래) 데이터를 걸러내어 백테스트 시
                    # 룩어헤드 편향(look-ahead bias)이 생기지 않게 한다.
                    if before <= date_dt <= curr_date_dt:
                        value = values[value_col_idx].strip()
                        result_data.append((date_dt, value))
                except (ValueError, IndexError):
                    continue

        # 날짜순으로 정렬한 뒤 출력 형식으로 정리
        result_data.sort(key=lambda x: x[0])

        ind_string = ""
        for date_dt, value in result_data:
            ind_string += f"{date_dt.strftime('%Y-%m-%d')}: {value}\n"

        if not ind_string:
            ind_string = "No data available for the specified date range.\n"

        result_str = (
            f"## {indicator.upper()} values from {before.strftime('%Y-%m-%d')} to {curr_date}:\n\n"
            + ind_string
            + "\n\n"
            + indicator_descriptions.get(indicator, "No description available.")
        )

        return result_str

    except AlphaVantageNotConfiguredError:
        # 벤더 사용 불가(API 키 없음). 이 예외를 그대로 전파해야 라우터(router)가
        # 다른 벤더로 폴백(fallback)하거나 "데이터 없음" 신호를 내보낼 수 있다.
        # 여기서 삼켜버리면 성공처럼 보이는 오류 문자열이 반환되어 버린다.
        raise
    except Exception as e:
        print(f"Error getting Alpha Vantage indicator data for {indicator}: {e}")
        return f"Error retrieving {indicator} data: {str(e)}"
