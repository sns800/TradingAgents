# ============================================================
# [모듈 개요] 종목 카탈로그 배치 수집기 (ECS Fargate 태스크 진입점)
#
# EventBridge 스케줄(평일 22:00 KST)이 워커 태스크 정의를 containerOverrides로
# 실행하며, 한국·일본·미국 상장 전 종목의 목록+기본정보를 수집해 S3에 씁니다.
#
# S3 저장 계약 (다른 작업자와 합의된 스펙 - 정확히 준수):
#  - 버킷: 환경변수 DATA_BUCKET
#  - 키: catalog/US.json.gz, catalog/KR.json.gz, catalog/JP.json.gz, catalog/meta.json
#  - 각 시장 파일(JSON, gzip):
#      {"market": "KR", "generated_at": "<ISO8601 UTC>", "count": N, "items": [...]}
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

MARKETS = ("US", "KR", "JP")

# 데이터 소스 URL (전부 무료 공개 자료)
NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
KRX_CORP_LIST_URL = "https://kind.krx.co.kr/corpgeneral/corpList.do?method=download"
JPX_LISTING_URL = (
    "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"
)

# 소스가 깨졌을 때(빈 파일, 형식 변경 등) 정상 카탈로그를 덮어쓰지 않기 위한
# 시장별 최소 종목 수 안전장치. 실측(2026-08): US ~6100, KR ~2700, JP ~3700.
MIN_ITEM_COUNT = {"US": 3000, "KR": 1500, "JP": 2000}

# 일부 공개 엔드포인트(KRX 등)는 기본 UA를 차단할 수 있어 브라우저형 UA를 보낸다.
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) TradingAgentsCatalog/1.0"}

ENRICH_CHUNK_SIZE = 200  # yf.download 일괄 요청당 티커 수


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fetch(url: str) -> bytes:
    resp = requests.get(url, headers=HTTP_HEADERS, timeout=120)
    resp.raise_for_status()
    return resp.content


# ---------- 시장별 수집 (다운로드 + 파싱) ----------

def collect_us() -> list[parsers.Item]:
    return parsers.parse_us(fetch(NASDAQ_LISTED_URL), fetch(OTHER_LISTED_URL))


def collect_kr() -> list[parsers.Item]:
    return parsers.parse_kr(fetch(KRX_CORP_LIST_URL))


def collect_jp() -> list[parsers.Item]:
    return parsers.parse_jp(fetch(JPX_LISTING_URL))


COLLECTORS = {"US": collect_us, "KR": collect_kr, "JP": collect_jp}


# ---------- 시세 보강 (yfinance) ----------

def enrich_prices(items: list[parsers.Item], chunk_size: int = ENRICH_CHUNK_SIZE) -> int:
    """yfinance 일괄 다운로드로 최근 종가를 item["price"]에 채운다.

    - 시장별 chunk_size개 청크로 yf.download를 호출하고, 주말·휴장일을 감안해
      최근 7일 창에서 최근 2영업일 이내의 마지막 유효 종가를 취한다.
    - 실패한 종목은 price=None으로 목록에 유지한다 (계약: null 허용).
    - 시가총액(market_cap)은 채우지 않는다(None 유지): yfinance에서 시총은
      종목당 개별 HTTP 호출(fast_info/quote)로만 얻을 수 있어 전체 카탈로그
      (~1.2만 종목) 기준 수만 건의 요청과 레이트리밋 대기가 필요하다.
      일 배치의 비용 대비 효용이 낮아 생략하고, 필요 시 개별 종목 화면에서
      실시간 조회로 보강하는 것을 전제로 한다.

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


# ---------- 저장 (S3 또는 로컬 dry-run) ----------

def make_payload(market: str, items: list[parsers.Item]) -> dict:
    """시장 파일 계약 형식의 페이로드를 만든다."""
    return {
        "market": market,
        "generated_at": now_iso(),
        "count": len(items),
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
        help="수집할 시장 (쉼표 구분, 기본: US,KR,JP)",
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
                filled = enrich_prices(items)
                logger.info("[%s] filled last close for %d/%d items", market, filled, len(items))
            payload = make_payload(market, items)
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
