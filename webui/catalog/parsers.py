# ============================================================
# [모듈 개요] 종목 카탈로그 파서 모음 (미국·한국·일본)
#
# 각 거래소가 공개하는 상장 종목 목록 파일(bytes/str)을 받아
# S3 카탈로그 계약의 item 딕셔너리 리스트로 변환합니다.
# 다운로드(네트워크)와 분리되어 있어 고정 픽스처로 단위 테스트가 가능합니다.
#
# item 스키마 (다른 작업자와 합의된 계약 - 키/형식 변경 금지):
#   {"ticker": "005930.KS", "name": "삼성전자", "market": "KOSPI",
#    "sector": "전기·전자", "industry": null, "price": 71000.0,
#    "currency": "KRW", "market_cap": 420000000000000.0}
# - 값이 없으면 null(None). ticker는 야후 파이낸스 형식.
# - price/market_cap은 파서 단계에서는 항상 None이며 시세 보강 단계에서 채웁니다.
# ============================================================
from __future__ import annotations

import io
from typing import Any

import pandas as pd

Item = dict[str, Any]

# otherlisted.txt의 Exchange 코드 -> 사람이 읽는 거래소 이름
# (출처: NASDAQ Trader Symbol Directory 명세)
US_EXCHANGE_NAMES = {
    "A": "NYSE American",
    "N": "NYSE",
    "P": "NYSE Arca",
    "Z": "Cboe BZX",
    "V": "IEX",
}

# KIND 상장법인목록의 시장구분 -> (카탈로그 market 이름, 야후 접미사)
# 코넥스(KONEX)는 야후 파이낸스가 시세를 제공하지 않고 계약상 접미사 정의도
# 없으므로 카탈로그에서 제외합니다.
KR_MARKET_MAP = {
    "유가": ("KOSPI", ".KS"),
    "코스닥": ("KOSDAQ", ".KQ"),
}

# JPX 상장종목일람의 市場・商品区分 키워드 -> 카탈로그 market 이름.
# "内国株式/外国株式"가 포함된 구분만 보통주(종류주식 포함)로 취급하고,
# ETF・ETN / REIT・ファンド류 / PRO Market / 出資証券은 제외합니다.
# (구분 값은 실제 data_j.xls에서 확인: プライム（内国株式）, スタンダード（内国株式）,
#  グロース（内国株式）, 같은 3개 시장의 （外国株式） 변형, ETF・ETN,
#  REIT・ベンチャーファンド・カントリーファンド・インフラファンド, PRO Market, 出資証券)
JP_MARKET_NAMES = [
    ("プライム", "Prime"),
    ("スタンダード", "Standard"),
    ("グロース", "Growth"),
]


def _make_item(ticker: str, name: str, market: str, sector: str | None, currency: str) -> Item:
    """카탈로그 계약 스키마의 item을 생성한다 (price/market_cap은 보강 전이라 None)."""
    return {
        "ticker": ticker,
        "name": name,
        "market": market,
        "sector": sector,
        "industry": None,
        "price": None,
        "currency": currency,
        "market_cap": None,
    }


def _ensure_text(raw: bytes | str, encoding: str = "utf-8") -> str:
    return raw.decode(encoding, errors="replace") if isinstance(raw, bytes) else raw


def _clean(value: Any) -> str | None:
    """pandas 셀 값을 정돈한다 (NaN/빈 문자열 -> None)."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    return text or None


# ---------- 미국 (NASDAQ Trader Symbol Directory) ----------

def _iter_pipe_rows(raw: bytes | str):
    """파이프(|) 구분 텍스트의 데이터 행을 순회한다.

    첫 줄(헤더)과 마지막 줄("File Creation Time: ...")은 건너뜁니다.
    """
    lines = _ensure_text(raw).splitlines()
    for line in lines[1:]:
        line = line.strip()
        if not line or line.startswith("File Creation Time"):
            continue
        yield [f.strip() for f in line.split("|")]


def _us_yahoo_ticker(symbol: str) -> str | None:
    """미국 심볼을 야후 파이낸스 형식으로 변환한다.

    - 클래스 주식: 점 표기 -> 대시 표기 (BRK.B -> BRK-B)
    - 우선주("$" 포함, 예: ABR$D): 야후 표기(-P 계열)가 비표준이고 분석 대상으로도
      가치가 낮아 카탈로그에서 제외합니다.
    """
    symbol = symbol.strip().upper()
    if not symbol or "$" in symbol:
        return None
    return symbol.replace(".", "-")


def parse_us(nasdaq_raw: bytes | str, other_raw: bytes | str) -> list[Item]:
    """nasdaqlisted.txt + otherlisted.txt -> US 카탈로그 item 리스트.

    Test Issue=Y(테스트 종목)와 ETF=Y는 제외합니다. 두 파일 모두 sector 정보가
    없으므로 sector는 None으로 두며, 시세 보강 단계에서도 채우지 않습니다
    (종목당 개별 조회가 필요해 비용이 과다).
    """
    items: list[Item] = []
    seen: set[str] = set()

    # nasdaqlisted.txt:
    # Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
    for fields in _iter_pipe_rows(nasdaq_raw):
        if len(fields) < 8:
            continue
        symbol, name, _category, test_issue, _fin, _lot, etf, _next_shares = fields[:8]
        if test_issue == "Y" or etf == "Y":
            continue
        ticker = _us_yahoo_ticker(symbol)
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        items.append(_make_item(ticker, name, "NASDAQ", None, "USD"))

    # otherlisted.txt (NYSE 등 비-나스닥 상장, ACT Symbol 기준):
    # ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol
    for fields in _iter_pipe_rows(other_raw):
        if len(fields) < 8:
            continue
        act_symbol, name, exchange, _cqs, etf, _lot, test_issue, _nasdaq_symbol = fields[:8]
        if test_issue == "Y" or etf == "Y":
            continue
        ticker = _us_yahoo_ticker(act_symbol)
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        market = US_EXCHANGE_NAMES.get(exchange, exchange or None) or "US"
        items.append(_make_item(ticker, name, market, None, "USD"))

    return items


# ---------- 한국 (KRX KIND 상장법인목록) ----------

def parse_kr(raw: bytes) -> list[Item]:
    """KIND 상장법인목록(.xls로 위장한 EUC-KR HTML 테이블) -> KR 카탈로그 item 리스트.

    시장구분 컬럼으로 유가(코스피) -> .KS / 코스닥 -> .KQ 접미사를 붙이고,
    코넥스는 제외합니다. 업종 컬럼을 sector로 사용합니다.
    """
    tables = pd.read_html(io.BytesIO(raw), encoding="euc-kr")
    if not tables:
        raise ValueError("KIND corp list HTML contains no table")
    df = tables[0]
    required = {"회사명", "시장구분", "종목코드", "업종"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"KIND corp list missing columns: {sorted(missing)}")

    items: list[Item] = []
    seen: set[str] = set()
    for row in df.to_dict("records"):
        market_raw = _clean(row.get("시장구분"))
        mapped = KR_MARKET_MAP.get(market_raw or "")
        if not mapped:
            continue  # 코넥스 등 계약 밖 시장
        market, suffix = mapped
        code = _clean(row.get("종목코드"))
        name = _clean(row.get("회사명"))
        if not code or not name:
            continue
        # 종목코드는 6자리(구형 숫자 6자리 또는 신형 영숫자 조합). HTML 파싱 과정에서
        # 앞자리 0이 떨어질 수 있어 6자리로 zero-pad 한다 (예: 5930 -> 005930).
        code = code.upper().zfill(6)
        ticker = f"{code}{suffix}"
        if ticker in seen:
            continue  # KIND 원본에 동일 행이 중복 수록되는 사례가 있다 (실측 37건)
        seen.add(ticker)
        items.append(_make_item(ticker, name, market, _clean(row.get("업종")), "KRW"))

    return items


# ---------- 일본 (JPX 상장종목일람) ----------

def parse_jp(raw: bytes) -> list[Item]:
    """JPX data_j.xls(레거시 .xls) -> JP 카탈로그 item 리스트.

    .xls 읽기는 xlrd 엔진을 사용합니다. xlrd 2.x는 .xlsx 지원을 제거했을 뿐
    레거시 .xls는 계속 지원하므로(2.0.2 + pandas 3.x로 실물 파일 검증 완료)
    1.2.0 핀 없이 최신 xlrd를 사용합니다.
    """
    df = pd.read_excel(io.BytesIO(raw))
    required = {"コード", "銘柄名", "市場・商品区分", "33業種区分"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"JPX listing file missing columns: {sorted(missing)}")

    items: list[Item] = []
    seen: set[str] = set()
    for row in df.to_dict("records"):
        segment = _clean(row.get("市場・商品区分")) or ""
        # 内国/外国 주식만 채택 -> ETF・ETN, REIT・ファンド류, PRO Market, 出資証券 제외
        if "内国株式" not in segment and "外国株式" not in segment:
            continue
        market = next((en for jp, en in JP_MARKET_NAMES if jp in segment), None)
        if market is None:
            continue
        code = _clean(row.get("コード"))
        name = _clean(row.get("銘柄名"))
        if not code or not name:
            continue
        # 엑셀 숫자 셀로 읽힌 경우 "1301.0" 형태를 방어한다. 코드는 4자리 숫자 또는
        # 영문 포함 신형 코드(예: 130A), 종류주식은 5자리(예: 25935)일 수 있다.
        code = code.upper().removesuffix(".0")
        ticker = f"{code}.T"
        if ticker in seen:
            continue  # 소스 중복 방어 (카탈로그 티커는 유일해야 한다)
        seen.add(ticker)
        sector = _clean(row.get("33業種区分"))
        if sector == "-":
            sector = None
        items.append(_make_item(ticker, name, market, sector, "JPY"))

    return items
