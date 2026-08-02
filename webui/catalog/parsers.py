# ============================================================
# [모듈 개요] 종목 카탈로그 파서 모음 (미국·한국·일본·중국)
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
import json
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

# 중국 A주 종목코드 접두사 3자리 -> (카탈로그 market 이름, 야후 접미사).
# 실제 규칙은 소스 데이터로 확인(2026-08): 상하이(SSE)는 60x·603·605(메인보드)와
# 688·689(과창판 STAR), 선전(SZSE)은 000·001·002·003(메인보드)과 300·301·302
# (창업판 ChiNext). B주(상하이 900xxx / 선전 200xxx)·기타 코드는 여기에 없어
# 자동 제외된다. 접미사는 야후 파이낸스 형식(.SS=상하이, .SZ=선전).
CN_CODE_PREFIX_MAP = {
    "600": ("Shanghai", ".SS"),
    "601": ("Shanghai", ".SS"),
    "603": ("Shanghai", ".SS"),
    "605": ("Shanghai", ".SS"),
    "688": ("STAR", ".SS"),
    "689": ("STAR", ".SS"),
    "000": ("Shenzhen", ".SZ"),
    "001": ("Shenzhen", ".SZ"),
    "002": ("Shenzhen", ".SZ"),
    "003": ("Shenzhen", ".SZ"),
    "300": ("ChiNext", ".SZ"),
    "301": ("ChiNext", ".SZ"),
    "302": ("ChiNext", ".SZ"),
}


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


# ---------- 중국 (SSE/SZSE 공식 상장사 목록) ----------

def _cn_lookup(code: str) -> tuple[str, str] | None:
    """6자리 A주 코드 -> (market, 야후 접미사). A주 보통주가 아니면 None."""
    if not (len(code) == 6 and code.isdigit()):
        return None
    return CN_CODE_PREFIX_MAP.get(code[:3])


def _append_cn_item(
    items: list[Item], seen: set[str], code: str | None, name: str | None, sector: str | None
) -> None:
    """코드/이름을 검증하고 A주 보통주면 CN item을 추가한다 (통화 CNY)."""
    if not code or not name:
        return
    mapped = _cn_lookup(code)
    if not mapped:
        return  # B주·기타 코드 등 계약 밖 종목
    market, suffix = mapped
    ticker = f"{code}{suffix}"
    if ticker in seen:
        return  # 소스 중복 방어 (카탈로그 티커는 유일해야 한다)
    seen.add(ticker)
    if sector == "-":
        sector = None
    items.append(_make_item(ticker, name, market, sector, "CNY"))


def _parse_sse(raw: bytes, items: list[Item], seen: set[str]) -> None:
    """상하이거래소(SSE) 상장사 JSON을 파싱해 items에 A주 보통주를 추가한다.

    출처: http://query.sse.com.cn 의 commonQuery(sqlId COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L)
    응답은 {"result": [ {A_STOCK_CODE, COMPANY_ABBR, CSRC_CODE_DESC, DELIST_DATE, ...} ]}.
    상장폐지(DELIST_DATE가 '-'가 아님) 종목은 제외하고, 업종은 CSRC 산업분류
    (CSRC_CODE_DESC)를 sector로 사용한다.
    """
    payload = json.loads(_ensure_text(raw))
    rows = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("SSE listing JSON has no result list")
    for row in rows:
        if not isinstance(row, dict):
            continue
        if _clean(row.get("DELIST_DATE")) not in (None, "-"):
            continue  # 상장폐지 종목 제외
        code = _clean(row.get("A_STOCK_CODE"))
        name = _clean(row.get("COMPANY_ABBR")) or _clean(row.get("SEC_NAME_CN"))
        _append_cn_item(items, seen, code, name, _clean(row.get("CSRC_CODE_DESC")))


def _parse_szse(raw: bytes, items: list[Item], seen: set[str]) -> None:
    """선전거래소(SZSE) A주 목록 xlsx를 파싱해 items에 A주 보통주를 추가한다.

    출처: http://www.szse.cn 의 상장사 A주 목록(ShowReport CATALOGID=1110, xlsx).
    이 목록은 이미 A주(주판·창업판)만 담고 있어 ETF·펀드·B주는 포함되지 않는다.
    업종은 '所属行业'(예: 'J 金融业')에서 앞의 CSRC 분류 문자를 떼어 sector로 쓴다.
    xlsx 읽기는 openpyxl 엔진을 사용한다.
    """
    df = pd.read_excel(io.BytesIO(raw), dtype=str)
    required = {"A股代码", "A股简称", "所属行业"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"SZSE listing file missing columns: {sorted(missing)}")
    for row in df.to_dict("records"):
        code = _clean(row.get("A股代码"))
        name = _clean(row.get("A股简称"))
        sector = _clean(row.get("所属行业"))
        if sector:
            # '所属行业'는 'J 金融业'처럼 CSRC 분류 문자+공백+한글명 형태다.
            parts = sector.split(None, 1)
            if len(parts) == 2 and len(parts[0]) == 1:
                sector = parts[1]
        _append_cn_item(items, seen, code, name, sector)


def parse_cn(sse_raw: bytes | str, szse_raw: bytes) -> list[Item]:
    """SSE 상장사 JSON + SZSE A주 xlsx -> CN 카탈로그 item 리스트.

    상하이(.SS)와 선전(.SZ)을 합쳐 반환한다. 두 소스 모두 A주 보통주만
    남기며(B주·상장폐지·기타 코드 제외), 티커는 6자리 코드 + 야후 접미사다.
    통화는 CNY. 미국(parse_us)과 같은 다중 소스 파서 형태다.
    """
    items: list[Item] = []
    seen: set[str] = set()
    _parse_sse(sse_raw, items, seen)
    _parse_szse(szse_raw, items, seen)
    return items
