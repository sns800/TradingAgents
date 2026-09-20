# ============================================================
# [모듈 개요] 종목 카탈로그 배치 수집기 (ECS Fargate 태스크 진입점)
#
# EventBridge 스케줄(평일 22:00 KST)이 워커 태스크 정의를 containerOverrides로
# 실행하며, 한국·일본·미국·중국 상장 전 종목의 목록+기본정보를 수집해 S3에 씁니다.
#
# S3 저장 계약 (다른 작업자와 합의된 스펙 - 정확히 준수):
#  - 버킷: 환경변수 DATA_BUCKET
#  - 키: catalog/US.json.gz, catalog/KR.json.gz, catalog/JP.json.gz,
#        catalog/CN.json.gz, catalog/meta.json
#  - 각 시장 파일(JSON, gzip):
#      {"market": "KR", "generated_at": "<ISO8601 UTC>", "count": N,
#       "enriched_at": "<ISO8601 UTC>|null", "items": [...]}
#    enriched_at은 시세·시가총액 조회 시점(--skip-enrich 시 null) — 계약에
#    추가된 확장 필드로, UI가 "시총 기준 시점"을 표기하는 데 쓴다.
#    item.name_ko(한국어 종목명, 일본·중국 대상)도 확장 필드다 — 원본 name은
#    변경하지 않고 네이버 증권에서 별도 수집한다 (parsers.py 계약 주석 참고).
#  - meta.json(비압축): {"markets": {"US": {"generated_at": "...", "count": N}, ...}}
#    부분 실패 시 성공한 시장만 갱신 (기존 meta를 읽어 병합).
#
# 실행 예:
#   python webui/catalog/build_catalog.py                  # 전 시장 수집 후 S3 업로드
#   python webui/catalog/build_catalog.py --markets KR,JP  # 일부 시장만
#   python webui/catalog/build_catalog.py --dry-run --skip-enrich  # 로컬 스모크
#
# 시장별 실패 격리: 한 시장의 다운로드/파싱/업로드가 실패해도 나머지 시장은
# 계속 진행하며, 모든 시장이 실패했을 때만 비0 종료 코드를 반환합니다.
# ============================================================
from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# 스크립트로 직접 실행되므로(sys.path[0] = 이 파일의 디렉토리) 같은 디렉토리의
# parsers 모듈을 평면 임포트한다. (webui/는 패키지가 아님 - worker.py와 동일 구조)
import parsers
import requests

# 일부 데이터 라이브러리(yfinance 등)의 내부 HTTP 호출에는 타임아웃이 없어,
# 응답 없는 소켓 읽기에서 배치 전체가 무한 대기할 수 있다. 전역 소켓 기본
# 타임아웃을 걸어 그런 호출을 예외로 바꾸면 시장별 실패 격리가 다음 시장으로
# 진행시킨다 (scripts/backtest.py에서 검증된 패턴). 명시적 타임아웃이 있는
# requests/botocore 호출에는 영향이 없다.
socket.setdefaulttimeout(120)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("catalog")

MARKETS = ("US", "KR", "JP", "CN")

# 데이터 소스 URL (전부 무료 공개 자료)
NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
# 미국 업종(sector) 소스: NASDAQ 스크리너. 상장 파일 2종에는 업종 정보가 없어
# 스크리너의 전 종목 덤프(단일 호출, NASDAQ·NYSE·AMEX 포함)에서 sector를 얻는다.
NASDAQ_SCREENER_URL = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=25&download=true"
KRX_CORP_LIST_URL = "https://kind.krx.co.kr/corpgeneral/corpList.do?method=download"
JPX_LISTING_URL = (
    "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"
)
# 중국: 상하이(SSE)·선전(SZSE) 공식 상장사 목록. 두 소스 모두 각 거래소 사이트를
# Referer로 요구하므로 collect_cn에서 전용 헤더로 받는다.
#  - SSE: 상장 A주 전체(메인보드+과창판) JSON 조회 엔드포인트.
#  - SZSE: A주 목록(주판+창업판) xlsx 다운로드(ShowReport CATALOGID=1110).
SSE_LISTING_URL = (
    "http://query.sse.com.cn/sseQuery/commonQuery.do"
    "?sqlId=COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L&isPagination=false"
)
SSE_REFERER = "http://www.sse.com.cn/"
SZSE_LISTING_URL = (
    "https://www.szse.cn/api/report/ShowReport"
    "?SHOWTYPE=xlsx&CATALOGID=1110&TABKEY=tab1&random=0.1"
)
SZSE_REFERER = "https://www.szse.cn/market/product/stock/list/index.html"

# 소스가 깨졌을 때(빈 파일, 형식 변경 등) 정상 카탈로그를 덮어쓰지 않기 위한
# 시장별 최소 종목 수 안전장치. 실측(2026-08): US ~6100, KR ~2700, JP ~3700,
# CN ~5200(SSE ~2350 + SZSE ~2900). CN 4000은 한 거래소 소스만 성공한
# 반쪽 결과(둘 중 큰 쪽 ~2900)도 걸러 낸다.
MIN_ITEM_COUNT = {"US": 3000, "KR": 1500, "JP": 2000, "CN": 4000}

# 일부 공개 엔드포인트(KRX 등)는 기본 UA를 차단할 수 있어 브라우저형 UA를 보낸다.
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) TradingAgentsCatalog/1.0"}

ENRICH_CHUNK_SIZE = 200  # yf.download 일괄 요청당 티커 수

# 야후 v7 quote(시가총액)는 차트 API보다 레이트리밋이 훨씬 엄격하다 (실측:
# 시세 보강의 차트 호출 ~1.9만 건 직후 96개 청크 전부 429). 청크 사이 지연과
# 레이트리밋 백오프 재시도로 방어하고, 시장별로 quote를 차트보다 먼저 돌린다.
QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
QUOTE_CHUNK_PAUSE = 1.0  # quote 청크 사이 지연 (초)
QUOTE_RETRY_WAITS = (30, 90, 180)  # 레이트리밋 백오프 (초, 청크당 최대 3회 재시도)

# 한국어 종목명(name_ko) 소스: 네이버 증권 해외주식 거래소별 목록.
# stockName이 한국식 표기(토요타자동차, 농업은행 등)이고 reutersCode가 우리
# 티커 형식(7203.T, 600000.SS)과 일치한다. 커버리지는 네이버 수록 종목 기준
# (실측 2026-08: 도쿄 3,982 / 상하이 1,827 / 선전 2,154)이라 소형주 일부는
# 빠질 수 있으며, 그 경우 name_ko=None으로 두고 UI는 원문만 표시한다.
NAVER_WORLDSTOCK_URL = "https://api.stock.naver.com/stock/exchange/{exchange}/marketValue"
NAVER_EXCHANGES = {"JP": ("TOKYO",), "CN": ("SHANGHAI", "SHENZHEN")}
NAVER_PAGE_SIZE = 100
NAVER_PAGE_PAUSE = 0.2  # 페이지 사이 지연 (초)


def _is_rate_limited(exc: Exception) -> bool:
    return type(exc).__name__ == "YFRateLimitError" or "Rate limited" in str(exc) \
        or "Too Many Requests" in str(exc)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fetch(url: str, extra_headers: dict | None = None) -> bytes:
    headers = dict(HTTP_HEADERS)
    if extra_headers:
        headers.update(extra_headers)  # 예: 중국 거래소가 요구하는 Referer
    resp = requests.get(url, headers=headers, timeout=120)
    resp.raise_for_status()
    return resp.content


# ---------- 시장별 수집 (다운로드 + 파싱) ----------

def collect_us() -> list[parsers.Item]:
    # 업종 소스(스크리너) 실패는 fail-open: 목록 수집은 계속하고 sector만 None이 된다.
    sector_map: dict[str, str] = {}
    try:
        sector_map = parsers.parse_us_sectors(fetch(NASDAQ_SCREENER_URL))
        logger.info("[US] screener sectors for %d tickers", len(sector_map))
    except Exception:
        logger.exception("[US] sector source failed, continuing without sectors")
    return parsers.parse_us(fetch(NASDAQ_LISTED_URL), fetch(OTHER_LISTED_URL), sector_map)


def collect_kr() -> list[parsers.Item]:
    return parsers.parse_kr(fetch(KRX_CORP_LIST_URL))


def collect_jp() -> list[parsers.Item]:
    return parsers.parse_jp(fetch(JPX_LISTING_URL))


def collect_cn() -> list[parsers.Item]:
    return parsers.parse_cn(
        fetch(SSE_LISTING_URL, {"Referer": SSE_REFERER}),
        fetch(SZSE_LISTING_URL, {"Referer": SZSE_REFERER}),
    )


COLLECTORS = {"US": collect_us, "KR": collect_kr, "JP": collect_jp, "CN": collect_cn}


# ---------- 시세 보강 (yfinance) ----------

def enrich_prices(items: list[parsers.Item], chunk_size: int = ENRICH_CHUNK_SIZE) -> int:
    """yfinance 일괄 다운로드로 최근 종가를 item["price"]에 채운다.

    - 시장별 chunk_size개 청크로 yf.download를 호출하고, 주말·휴장일을 감안해
      최근 7일 창에서 최근 2영업일 이내의 마지막 유효 종가를 취한다.
    - 실패한 종목은 price=None으로 목록에 유지한다 (계약: null 허용).
    - 시가총액(market_cap)은 여기서 채우지 않는다 — enrich_market_caps가
      야후 v7 배치 quote로 별도 수집한다.

    반환값: 종가를 채운 종목 수.
    """
    import yfinance as yf  # 무거운 임포트라 보강 단계에서만 로드

    filled = 0
    tickers = [item["ticker"] for item in items]
    by_ticker = {item["ticker"]: item for item in items}
    for start in range(0, len(tickers), chunk_size):
        chunk = tickers[start : start + chunk_size]
        try:
            frame = yf.download(
                tickers=chunk,
                period="7d",  # 주말·연휴를 덮는 최소 창 (최근 2영업일 확보 목적)
                interval="1d",
                group_by="ticker",
                auto_adjust=False,
                progress=False,
                threads=True,
            )
        except Exception as exc:  # noqa: BLE001 - 청크 실패는 격리하고 계속
            logger.warning("price chunk %d-%d failed: %s", start, start + len(chunk), exc)
            continue
        if frame is None or frame.empty:
            continue
        for ticker in chunk:
            # group_by="ticker"면 단일 티커도 (티커, 필드) MultiIndex 컬럼으로
            # 온다(yfinance 1.5 실측). 버전에 따라 평면 컬럼일 수 있어 폴백을 둔다.
            try:
                closes = frame[ticker]["Close"].dropna()
            except KeyError:
                try:
                    closes = frame["Close"].dropna() if len(chunk) == 1 else None
                except KeyError:
                    closes = None
            if closes is None:
                continue
            if closes.empty:
                continue
            by_ticker[ticker]["price"] = round(float(closes.iloc[-1]), 6)
            filled += 1
    return filled


def _fetch_quote_chunk(ydata, chunk: list[str]) -> list[dict]:
    """야후 v7 quote 한 청크를 조회한다. 레이트리밋이면 백오프 후 재시도."""
    last_exc: Exception | None = None
    for wait in (0,) + QUOTE_RETRY_WAITS:
        if wait:
            logger.info("quote rate limited, retrying in %ds", wait)
            time.sleep(wait)
        try:
            resp = ydata.get_raw_json(
                QUOTE_URL,
                params={
                    "symbols": ",".join(chunk),
                    "fields": "marketCap,regularMarketPrice",
                },
            )
            return (resp.get("quoteResponse") or {}).get("result") or []
        except Exception as exc:  # noqa: BLE001 - 레이트리밋만 재시도, 나머지는 즉시 전파
            last_exc = exc
            if not _is_rate_limited(exc):
                raise
    raise last_exc


def enrich_market_caps(items: list[parsers.Item], chunk_size: int = ENRICH_CHUNK_SIZE) -> int:
    """야후 v7 배치 quote로 item["market_cap"]을 채운다.

    시가총액은 yf.download(차트 API)로는 얻을 수 없지만, v7 quote 엔드포인트는
    심볼 수백 개 단위 일괄 조회를 지원해 전체 카탈로그도 ~100회 호출이면 된다.
    쿠키+크럼 인증이 필요해 yfinance의 YfData 세션을 재사용한다(4개 시장 전부
    거래 통화 기준 marketCap 반환을 실측 확인). quote 응답에 현재가가 있으면
    아직 비어 있는 price도 보충한다. 청크 실패는 격리하고 계속한다.

    반환값: 시가총액을 채운 종목 수.
    """
    from yfinance.data import YfData  # 무거운 임포트라 보강 단계에서만 로드

    ydata = YfData()
    by_ticker = {item["ticker"]: item for item in items}
    tickers = list(by_ticker)
    filled = 0
    for start in range(0, len(tickers), chunk_size):
        chunk = tickers[start : start + chunk_size]
        if start:
            time.sleep(QUOTE_CHUNK_PAUSE)  # 레이트리밋 예방용 청크 간 지연
        try:
            quotes = _fetch_quote_chunk(ydata, chunk)
        except Exception as exc:  # noqa: BLE001 - 청크 실패는 격리하고 계속
            logger.warning("market cap chunk %d-%d failed: %s", start, start + len(chunk), exc)
            continue
        for quote in quotes:
            if not isinstance(quote, dict):
                continue
            item = by_ticker.get(str(quote.get("symbol") or ""))
            if item is None:
                continue
            cap = quote.get("marketCap")
            if isinstance(cap, (int, float)) and cap > 0:
                item["market_cap"] = float(cap)
                filled += 1
            price = quote.get("regularMarketPrice")
            if item.get("price") is None and isinstance(price, (int, float)):
                item["price"] = round(float(price), 6)
    return filled


def fetch_naver_names(market: str) -> dict[str, str]:
    """네이버 해외주식 목록에서 {티커: 한국어 종목명} 매핑을 수집한다.

    시장에 대응하는 거래소가 없으면(한국·미국) 빈 매핑을 반환한다.
    페이지네이션으로 전 종목을 순회하며, 응답의 reutersCode를 그대로
    티커 키로 쓴다 (대문자 정규화만 수행).
    """
    names: dict[str, str] = {}
    for exchange in NAVER_EXCHANGES.get(market, ()):
        page = 1
        while True:
            resp = requests.get(
                NAVER_WORLDSTOCK_URL.format(exchange=exchange),
                params={"page": page, "pageSize": NAVER_PAGE_SIZE},
                headers=HTTP_HEADERS,
                timeout=120,
            )
            resp.raise_for_status()
            payload = resp.json()
            stocks = payload.get("stocks") or []
            for stock in stocks:
                if not isinstance(stock, dict):
                    continue
                ticker = str(stock.get("reutersCode") or "").strip().upper()
                name_ko = str(stock.get("stockName") or "").strip()
                if ticker and name_ko:
                    names[ticker] = name_ko
            total = int(payload.get("totalCount") or 0)
            if not stocks or page * NAVER_PAGE_SIZE >= total:
                break
            page += 1
            time.sleep(NAVER_PAGE_PAUSE)
    return names


def enrich_korean_names(market: str, items: list[parsers.Item]) -> int:
    """일본·중국 종목의 item["name_ko"]를 네이버 한국어 종목명으로 채운다.

    원본 name은 절대 수정하지 않는다 (별도 컬럼 계약). 네이버 미수록 종목과
    소스 실패는 fail-open: name_ko=None 유지, UI는 원문만 표시한다.

    반환값: 한국어 이름을 채운 종목 수.
    """
    try:
        names = fetch_naver_names(market)
    except Exception:  # noqa: BLE001 - 이름 소스 실패가 배치를 막으면 안 된다
        logger.exception("[%s] korean name source failed, continuing without names", market)
        return 0
    if not names:
        return 0
    filled = 0
    for item in items:
        name_ko = names.get(str(item["ticker"]).upper())
        if name_ko and name_ko != item.get("name"):
            item["name_ko"] = name_ko
            filled += 1
    return filled


# ---------- 저장 (S3 또는 로컬 dry-run) ----------

def make_payload(market: str, items: list[parsers.Item], enriched: bool) -> dict:
    """시장 파일 계약 형식의 페이로드를 만든다."""
    return {
        "market": market,
        "generated_at": now_iso(),
        "count": len(items),
        "enriched_at": now_iso() if enriched else None,
        "items": items,
    }


def gzip_json(payload: dict) -> bytes:
    return gzip.compress(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def s3_client():
    import boto3

    # 시스템 리소스(S3)는 서울 리전. 태스크 정의의 AWS_REGION은 Bedrock용
    # us-east-1이므로 worker.py와 동일하게 HOME_REGION을 명시적으로 사용한다.
    return boto3.client("s3", region_name=os.environ.get("HOME_REGION", "ap-northeast-2"))


def upload_market(s3, bucket: str, market: str, payload: dict) -> None:
    s3.put_object(
        Bucket=bucket,
        Key=f"catalog/{market}.json.gz",
        Body=gzip_json(payload),
        ContentType="application/json",
        ContentEncoding="gzip",
    )


def update_meta(s3, bucket: str, results: dict[str, dict]) -> None:
    """meta.json을 부분 병합으로 갱신한다 (성공한 시장만 덮어씀)."""
    meta: dict = {"markets": {}}
    try:
        obj = s3.get_object(Bucket=bucket, Key="catalog/meta.json")
        existing = json.loads(obj["Body"].read())
        if isinstance(existing.get("markets"), dict):
            meta["markets"] = existing["markets"]
    except Exception as exc:  # noqa: BLE001 - 최초 실행(NoSuchKey) 등은 빈 meta에서 시작
        logger.info("no existing meta.json, starting fresh (%s)", type(exc).__name__)
    for market, summary in results.items():
        meta["markets"][market] = summary
    s3.put_object(
        Bucket=bucket,
        Key="catalog/meta.json",
        Body=json.dumps(meta, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json",
    )


def write_local(output_dir: Path, market: str, payload: dict) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{market}.json.gz"
    path.write_bytes(gzip_json(payload))
    return path


def write_local_meta(output_dir: Path, results: dict[str, dict]) -> Path:
    path = output_dir / "meta.json"
    path.write_text(
        json.dumps({"markets": results}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


# ---------- 진입점 ----------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="상장 종목 카탈로그 수집 배치")
    parser.add_argument(
        "--markets",
        default=",".join(MARKETS),
        help="수집할 시장 (쉼표 구분, 기본: US,KR,JP,CN)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="S3 대신 --output-dir에 결과 파일을 쓴다 (로컬 스모크용)",
    )
    parser.add_argument(
        "--output-dir",
        default="catalog_out",
        help="--dry-run 결과를 쓸 디렉토리 (기본: ./catalog_out)",
    )
    parser.add_argument(
        "--skip-enrich",
        action="store_true",
        help="yfinance 시세 보강을 건너뛴다 (파서 스모크용, price=null 유지)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    markets = [m.strip().upper() for m in args.markets.split(",") if m.strip()]
    unknown = [m for m in markets if m not in COLLECTORS]
    if unknown:
        logger.error("unknown markets: %s (supported: %s)", unknown, list(COLLECTORS))
        return 2

    s3 = bucket = None
    if not args.dry_run:
        bucket = os.environ["DATA_BUCKET"]
        s3 = s3_client()

    results: dict[str, dict] = {}
    for market in markets:
        # 시장별 실패 격리: 어떤 시장이 실패해도 나머지 시장은 계속 진행한다.
        try:
            logger.info("[%s] collecting listing ...", market)
            items = COLLECTORS[market]()
            if len(items) < MIN_ITEM_COUNT[market]:
                raise ValueError(
                    f"suspiciously few items for {market}: "
                    f"{len(items)} < {MIN_ITEM_COUNT[market]} (source may be broken)"
                )
            logger.info("[%s] parsed %d items", market, len(items))
            if not args.skip_enrich:
                names = enrich_korean_names(market, items)
                logger.info("[%s] filled korean name for %d/%d items", market, names, len(items))
                # quote(시총)를 차트(시세)보다 먼저: 차트 호출 ~수천 건이 quote의
                # 엄격한 레이트리밋을 먼저 소진하는 것을 피한다. 가격은 이후
                # enrich_prices의 최근 종가가 quote의 현재가를 덮어쓴다.
                caps = enrich_market_caps(items)
                logger.info("[%s] filled market cap for %d/%d items", market, caps, len(items))
                filled = enrich_prices(items)
                logger.info("[%s] filled last close for %d/%d items", market, filled, len(items))
            payload = make_payload(market, items, enriched=not args.skip_enrich)
            if args.dry_run:
                path = write_local(Path(args.output_dir), market, payload)
                logger.info("[%s] wrote %s", market, path)
            else:
                upload_market(s3, bucket, market, payload)
                logger.info("[%s] uploaded s3://%s/catalog/%s.json.gz", market, bucket, market)
            results[market] = {"generated_at": payload["generated_at"], "count": payload["count"]}
        except Exception:
            logger.exception("[%s] failed, continuing with remaining markets", market)

    if results:
        if args.dry_run:
            logger.info("meta written to %s", write_local_meta(Path(args.output_dir), results))
        else:
            update_meta(s3, bucket, results)
            logger.info("meta.json updated for markets: %s", sorted(results))

    failed = [m for m in markets if m not in results]
    if failed:
        logger.error("failed markets: %s", failed)
    # 전 시장 실패 시에만 비0 종료 (부분 성공은 성공으로 간주 - meta가 부분 갱신됨)
    return 0 if results else 1


if __name__ == "__main__":
    sys.exit(main())
