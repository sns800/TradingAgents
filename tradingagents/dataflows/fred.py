# =============================================================================
# [모듈 개요 - 초보자용 안내]
# 이 파일은 미국 세인트루이스 연방준비은행이 제공하는 FRED(Federal Reserve
# Economic Data) API에서 거시경제(macro) 지표를 가져오는 모듈입니다.
# 기준금리, 국채 수익률, 물가(CPI), 고용, GDP 같은 시계열 데이터를 조회해
# 마크다운(markdown) 리포트로 정리합니다.
# TradingAgents(LLM 멀티 에이전트 주식 트레이딩 프레임워크)에서 뉴스 애널리스트
# 에이전트가 거시경제 논평을 실제 수치에 근거해 작성할 때 사용합니다.
# =============================================================================
"""FRED(Federal Reserve Economic Data) 거시경제 데이터 벤더.

세인트루이스 연준의 무료 API에서 거시경제 시계열 — 정책 금리, 국채 수익률,
인플레이션, 고용, 성장 — 을 가져온다. 뉴스 애널리스트가 헤드라인에만 의존하지
않고 실제 수치에 근거해 거시경제 논평을 하도록 돕는 용도다.

무료 API 키(https://fred.stlouisfed.org/docs/api/api_key.html)는 환경변수
``FRED_API_KEY`` 에서 읽으며, 설정되지 않았으면 ``FredNotConfiguredError`` 를
발생시켜 라우팅 계층이 프로그램 강제 종료가 아닌 "사용 불가(unavailable)"로
처리하게 한다.
"""
import logging
import os
from datetime import datetime, timedelta

import requests

from .errors import VendorNotConfiguredError

logger = logging.getLogger(__name__)

FRED_API_BASE = "https://api.stlouisfed.org/fred"

# 네트워크 타임아웃(timeout, 초 단위). 응답이 멈춘 요청이 에이전트를 무한정
# 멈추게 하지 않도록 제한한다. Alpha Vantage 클라이언트와 동일한 방식.
REQUEST_TIMEOUT = 30

# 호출자가 기간을 지정하지 않았을 때의 기본 조회 기간(일수). 1년이면 대부분의
# 월간/분기 시계열에서 추세와 전년 동기 대비(YoY) 기준점을 함께 볼 수 있다.
DEFAULT_LOOKBACK_DAYS = 365

# 렌더링되는 표의 최대 행 수. 의사결정에는 최근 값이 가장 중요하고, 일간
# 시계열(수익률, VIX)을 긴 기간으로 조회하면 LLM 컨텍스트가 넘쳐나기 때문.
MAX_ROWS = 40

# 사람이 쓰기 편한 별칭(alias) -> FRED 시리즈 ID 매핑(엄선된 목록). 여기에 없는
# 입력은 원시 FRED 시리즈 ID로 그대로 사용되므로, 고급 사용자가 이 목록에
# 제한되는 일은 없다.
MACRO_SERIES = {
    # 정책 금리 & 국채 수익률
    "fed_funds_rate": "FEDFUNDS",
    "federal_funds_rate": "FEDFUNDS",
    "fed_funds": "FEDFUNDS",
    "2y_treasury": "DGS2",
    "10y_treasury": "DGS10",
    "30y_treasury": "DGS30",
    "10y_2y_spread": "T10Y2Y",
    "yield_curve": "T10Y2Y",
    # 인플레이션
    "cpi": "CPIAUCSL",
    "core_cpi": "CPILFESL",
    "pce": "PCEPI",
    "core_pce": "PCEPILFE",
    "inflation_expectations": "T10YIE",
    # 성장 & 생산
    "real_gdp": "GDPC1",
    "gdp": "GDP",
    "industrial_production": "INDPRO",
    # 고용
    "unemployment_rate": "UNRATE",
    "unemployment": "UNRATE",
    "nonfarm_payrolls": "PAYEMS",
    "payrolls": "PAYEMS",
    "initial_claims": "ICSA",
    # 통화 & 시장
    "m2": "M2SL",
    "money_supply": "M2SL",
    "vix": "VIXCLS",
    "dollar_index": "DTWEXBGS",
    # 심리 & 주택
    "consumer_sentiment": "UMCSENT",
    "housing_starts": "HOUST",
    "retail_sales": "RSAFS",
}


class FredNotConfiguredError(VendorNotConfiguredError):
    """FRED가 선택되었지만 API 키가 설정되지 않았을 때 발생하는 예외.

    VendorNotConfiguredError(즉, 여전히 ValueError)이기도 하므로, 라우팅 계층의
    "벤더 사용 불가(vendor unavailable)" 처리와 기존에 ValueError를 잡던 호출부가
    모두 그대로 동작한다.
    """


def get_api_key() -> str:
    """환경변수에서 FRED API 키를 가져온다."""
    api_key = os.getenv("FRED_API_KEY")
    if not api_key:
        raise FredNotConfiguredError(
            "FRED_API_KEY environment variable is not set. Get a free key at "
            "https://fred.stlouisfed.org/docs/api/api_key.html."
        )
    return api_key


def _resolve_series_id(indicator: str) -> str:
    """친숙한 별칭(alias)을 FRED 시리즈 ID로 변환하거나, 원시 ID는 그대로 통과시킨다.

    입력이 알려진 별칭도 아니고 그럴듯한 시리즈 ID도 아닐 때 — 보통 LLM이 대신
    넘긴 서술형 문구(예: "bank of japan rate") — ``ValueError`` 를 발생시킨다.
    FRED ID는 짧은 영숫자이므로, API에서 400 오류가 나게 두는 대신 안내 문구와
    함께 미리 거부한다.
    """
    key = indicator.strip().lower().replace(" ", "_").replace("-", "_")
    if key in MACRO_SERIES:
        return MACRO_SERIES[key]
    candidate = indicator.strip().upper()
    # FRED 시리즈 ID는 공백이 없고 짧다; 그 외의 입력(LLM이 넘긴 서술형 문구)은
    # API에서 400 오류가 나게 두지 않고 여기서 거부한다.
    if not candidate or len(candidate) > 30 or any(c.isspace() for c in candidate):
        raise ValueError(
            f"'{indicator}' is not a known macro alias or a valid FRED series ID. "
            f"Use an alias (e.g. 'cpi', 'unemployment', '10y_treasury') or a raw "
            f"FRED series ID (e.g. 'CPIAUCSL')."
        )
    return candidate


def _request(path: str, params: dict) -> dict:
    """FRED 엔드포인트에 GET 요청을 보내고, 잘못된 요청 시 FRED의 JSON 오류 본문을 드러낸다."""
    api_params = {**params, "api_key": get_api_key(), "file_type": "json"}
    response = requests.get(
        f"{FRED_API_BASE}/{path}", params=api_params, timeout=REQUEST_TIMEOUT
    )
    # FRED는 알 수 없는 시리즈 ID나 잘못된 파라미터에 대해 400 상태 코드와
    # {"error_message": ...} 형태의 JSON을 반환한다; 이를 명확하고 조치 가능한
    # 오류로 바꿔준다.
    if response.status_code == 400:
        try:
            message = response.json().get("error_message", response.text)
        except ValueError:
            message = response.text
        raise ValueError(f"FRED request failed: {message}")
    response.raise_for_status()
    return response.json()


def get_macro_data(
    indicator: str,
    curr_date: str,
    look_back_days: int | None = None,
) -> str:
    """FRED 거시경제 시계열을 가져와 정리된 마크다운(markdown) 리포트로 반환한다.

    Args:
        indicator: 친숙한 별칭(예: "cpi", "unemployment", "10y_treasury") 또는
            원시 FRED 시리즈 ID(예: "CPIAUCSL", "DGS10").
        curr_date: 조회 기간의 끝(yyyy-mm-dd); 이 날짜 이후의 관측값은 반환하지
            않으므로, 과거 날짜로 조회해도 미래 데이터가 새어 들어오지 않는다
            (룩어헤드 편향(look-ahead bias) 방지).
        look_back_days: 과거 조회 기간(일수); ``None`` 이면 DEFAULT_LOOKBACK_DAYS 사용.

    Returns:
        시리즈 제목, 단위, 주기, 최신 값, 기간 내 변화량, 최근 관측값 표가 담긴
        마크다운 리포트.
    """
    if look_back_days is None:
        look_back_days = DEFAULT_LOOKBACK_DAYS

    end_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    start_date = (end_dt - timedelta(days=look_back_days)).strftime("%Y-%m-%d")

    # LLM이 잘못된 indicator를 넘긴 경우: 예외를 던지지 않고 안내 문구를 반환해,
    # 잘못된 인자 하나가 실행 전체를 중단시키지 않게 한다 (라우팅 계층도 거시
    # 데이터를 우아하게 저하(degrade)시키지만, 구체적인 메시지가 애널리스트에게
    # 더 유용하다).
    try:
        series_id = _resolve_series_id(indicator)
    except ValueError as e:
        return f"FRED: {e}"

    meta = _request("series", {"series_id": series_id}).get("seriess") or []
    if not meta:
        return (
            f"FRED series '{series_id}' not found. Pass a known alias "
            f"(e.g. 'cpi', 'unemployment') or a valid FRED series ID."
        )
    info = meta[0]
    title = info.get("title", series_id)
    units = info.get("units_short") or info.get("units", "")
    frequency = info.get("frequency", "")
    seasonal = info.get("seasonal_adjustment_short", "")

    observations = _request(
        "series/observations",
        {
            "series_id": series_id,
            "observation_start": start_date,
            "observation_end": curr_date,
            "sort_order": "asc",
        },
    ).get("observations", [])

    # FRED는 결측 관측값을 "."으로 표기한다.
    points = [
        (o["date"], o["value"])
        for o in observations
        if o.get("value") not in (".", None, "")
    ]

    header = (
        f"## FRED: {title} ({series_id})\n"
        f"- Units: {units}\n"
        f"- Frequency: {frequency}"
        f"{f' ({seasonal})' if seasonal else ''}\n"
        f"- Window: {start_date} to {curr_date}\n"
    )

    if not points:
        return header + (
            f"\nNo observations for {series_id} in this window. The series may "
            f"report less frequently than the window length; widen look_back_days."
        )

    first_date, first_val = points[0]
    last_date, last_val = points[-1]
    try:
        delta = float(last_val) - float(first_val)
        base = float(first_val)
        pct = f" ({delta / base * 100:+.2f}%)" if base != 0 else ""
        summary = (
            f"\n**Latest:** {last_val} ({last_date}) | "
            f"**Change over window:** {delta:+.2f}{pct} "
            f"from {first_val} ({first_date})\n"
        )
    except ValueError:
        summary = f"\n**Latest:** {last_val} ({last_date})\n"

    shown = points
    note = ""
    if len(points) > MAX_ROWS:
        shown = points[-MAX_ROWS:]
        note = f"\n_(showing the most recent {MAX_ROWS} of {len(points)} observations)_\n"

    table = (
        "\n| Date | Value |\n| --- | --- |\n"
        + "\n".join(f"| {d} | {v} |" for d, v in shown)
        + "\n"
    )

    return header + summary + note + table
