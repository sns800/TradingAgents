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
# - 해외(일본·중국·미국) sector는 "한글번역(원문)" 표기입니다 (예: "은행업(銀行業)").
#   매핑에 없는 새 분류 값은 원문 그대로 통과시킵니다 (fail-open).
# - name_ko: 한국어 종목명 (일본·중국 종목 대상, 계약에 추가된 확장 필드).
#   원본 name은 절대 바꾸지 않고 별도 컬럼으로 둔다. 파서 단계에서는 항상
#   None이며 배치의 한글명 보강 단계(네이버 증권)에서 채운다. UI는
#   name_ko가 있으면 "한글명(원문)"으로 표시하고 없으면 원문만 표시한다.
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


# ---------- 업종 한글 표기 ----------
# 해외 시장 업종을 "한글번역(원문)" 형태로 표기하기 위한 시장별 매핑.
# 값은 각 소스의 공식 분류 체계 전체를 담는다. 분류 개편으로 새 값이 오면
# _localize_sector가 원문을 그대로 반환하므로 배치는 깨지지 않는다.

# JPX 33업종 구분 (東証 33業種, data_j.xls의 "33業種区分" 전체 33종)
JP_SECTOR_KO = {
    "水産・農林業": "수산·농림업",
    "鉱業": "광업",
    "建設業": "건설업",
    "食料品": "식료품",
    "繊維製品": "섬유제품",
    "パルプ・紙": "펄프·종이",
    "化学": "화학",
    "医薬品": "의약품",
    "石油・石炭製品": "석유·석탄제품",
    "ゴム製品": "고무제품",
    "ガラス・土石製品": "유리·토석제품",
    "鉄鋼": "철강",
    "非鉄金属": "비철금속",
    "金属製品": "금속제품",
    "機械": "기계",
    "電気機器": "전기기기",
    "輸送用機器": "수송용기기",
    "精密機器": "정밀기기",
    "その他製品": "기타제품",
    "電気・ガス業": "전기·가스업",
    "陸運業": "육상운송업",
    "海運業": "해운업",
    "空運業": "항공운송업",
    "倉庫・運輸関連業": "창고·운수관련업",
    "情報・通信業": "정보·통신업",
    "卸売業": "도매업",
    "小売業": "소매업",
    "銀行業": "은행업",
    "証券、商品先物取引業": "증권·상품선물거래업",
    "保険業": "보험업",
    "その他金融業": "기타금융업",
    "不動産業": "부동산업",
    "サービス業": "서비스업",
}

# CSRC 산업분류. SSE는 门类 정식 명칭, SZSE는 자체 축약형을 쓴다
# (두 계열 모두 2026-08 실데이터에서 관측된 값 전체 + SSE 정식 门类 잔여분).
CN_SECTOR_KO = {
    # SSE 정식 门类 명칭
    "农、林、牧、渔业": "농림축산어업",
    "采矿业": "광업",
    "制造业": "제조업",
    "电力、热力、燃气及水生产和供应业": "전기·열·가스·수도공급업",
    "建筑业": "건설업",
    "批发和零售业": "도소매업",
    "交通运输、仓储和邮政业": "운수·창고·우편업",
    "住宿和餐饮业": "숙박·요식업",
    "信息传输、软件和信息技术服务业": "정보통신·소프트웨어·IT서비스업",
    "金融业": "금융업",
    "房地产业": "부동산업",
    "租赁和商务服务业": "임대·비즈니스서비스업",
    "科学研究和技术服务业": "과학연구·기술서비스업",
    "水利、环境和公共设施管理业": "수자원·환경·공공시설관리업",
    "居民服务、修理和其他服务业": "주민서비스·수리·기타서비스업",
    "教育": "교육",
    "卫生和社会工作": "보건·사회복지업",
    "文化、体育和娱乐业": "문화·체육·엔터테인먼트업",
    "综合": "종합",
    # SZSE 축약형
    "农林牧渔": "농림축산어업",
    "住宿餐饮": "숙박·요식업",
    "信息技术": "정보기술",
    "公共环保": "공공·환경보호",
    "卫生": "보건",
    "商务服务": "비즈니스서비스",
    "居民服务": "주민서비스",
    "房地产": "부동산",
    "批发零售": "도소매업",
    "文化传播": "문화·미디어",
    "水电煤气": "수도·전기·가스",
    "科研服务": "과학연구서비스",
    "运输仓储": "운수·창고",
}

# NASDAQ 스크리너(screener/stocks)의 sector 분류 전체 12종
US_SECTOR_KO = {
    "Technology": "기술",
    "Telecommunications": "통신",
    "Health Care": "헬스케어",
    "Finance": "금융",
    "Real Estate": "부동산",
    "Consumer Discretionary": "임의소비재",
    "Consumer Staples": "필수소비재",
    "Industrials": "산업재",
    "Basic Materials": "소재",
    "Energy": "에너지",
    "Utilities": "유틸리티",
    "Miscellaneous": "기타",
}


def _localize_sector(mapping: dict[str, str], sector: str | None) -> str | None:
    """업종 원문을 "한글번역(원문)" 표기로 바꾼다. 매핑에 없으면 원문 유지."""
    if not sector:
        return None
    ko = mapping.get(sector)
    return f"{ko}({sector})" if ko else sector


def _make_item(ticker: str, name: str, market: str, sector: str | None, currency: str) -> Item:
    """카탈로그 계약 스키마의 item을 생성한다 (price/market_cap/name_ko는 보강 전이라 None)."""
    return {
        "ticker": ticker,
        "name": name,
        "name_ko": None,
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


def parse_us_sectors(raw: bytes | str) -> dict[str, str]:
    """NASDAQ 스크리너 JSON -> {야후 티커: 업종 원문(영어)} 매핑.

    스크리너의 심볼 표기를 야후 형식으로 정규화한다: 클래스 주식은 슬래시
    (BRK/A -> BRK-A), 우선주는 캐럿(ABR^D)인데 우선주는 카탈로그 대상이
    아니므로 버린다. sector가 빈 종목(권리·유닛·워런트 등)은 매핑에서 제외.
    """
    payload = json.loads(_ensure_text(raw))
    rows = (payload.get("data") or {}).get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("NASDAQ screener JSON has no data.rows list")
    sectors: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "")
        if "^" in symbol:
            continue  # 우선주 (카탈로그 제외 대상)
        ticker = _us_yahoo_ticker(symbol.replace("/", "."))
        sector = _clean(row.get("sector"))
        if ticker and sector:
            sectors[ticker] = sector
    return sectors


def parse_us(
    nasdaq_raw: bytes | str,
    other_raw: bytes | str,
    sector_map: dict[str, str] | None = None,
) -> list[Item]:
    """nasdaqlisted.txt + otherlisted.txt -> US 카탈로그 item 리스트.

    Test Issue=Y(테스트 종목)와 ETF=Y는 제외합니다. 두 상장 파일에는 sector
    정보가 없어 parse_us_sectors(NASDAQ 스크리너)의 {티커: 업종} 매핑을 받아
    "한글번역(원문)"으로 채웁니다. 매핑이 없거나(스크리너 실패 시 fail-open)
    티커가 매핑에 없으면 sector는 None입니다.
    """
    sector_map = sector_map or {}

    def _sector(ticker: str) -> str | None:
        return _localize_sector(US_SECTOR_KO, sector_map.get(ticker))

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
        items.append(_make_item(ticker, name, "NASDAQ", _sector(ticker), "USD"))

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
        items.append(_make_item(ticker, name, market, _sector(ticker), "USD"))

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
        items.append(_make_item(ticker, name, market, _localize_sector(JP_SECTOR_KO, sector), "JPY"))

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
    items.append(_make_item(ticker, name, market, _localize_sector(CN_SECTOR_KO, sector), "CNY"))


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
