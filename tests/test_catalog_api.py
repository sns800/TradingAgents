# ============================================================
# [테스트 개요] 웹 UI 종목 카탈로그 API + 실행 생성 시 티커 검증
#
# webui/backend/api_handler.py 를 boto3 모킹 상태로 로드해 검증한다.
#  - GET /api/catalog: 검색(q)/업종(sector)/정렬/페이지네이션/sectors 목록
#  - 카탈로그 미생성 시 404, 컨테이너 전역 캐시(두 번째 호출은 S3 미호출)
#  - POST /api/runs: 카탈로그 정확 일치 / 특수자산 화이트리스트 /
#    후보 제안(400) / fail-open, 그리고 정규 티커 치환(brk.b → BRK-B)
# ============================================================
import gzip
import importlib.util
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

HANDLER_PATH = Path(__file__).resolve().parents[1] / "webui" / "backend" / "api_handler.py"

_ENV = {
    "TABLE_NAME": "runs-table",
    "DATA_BUCKET": "data-bucket",
    "CLUSTER_ARN": "arn:aws:ecs:cluster/test",
    "TASK_DEF": "worker-task",
    "SUBNET_IDS": "subnet-1,subnet-2",
    "SECURITY_GROUP": "sg-1",
    "COGNITO_CLIENT_ID": "client-id",
}


class FakeS3:
    """catalog/*.json.gz 를 흉내 내는 최소 S3 클라이언트 (호출 횟수 기록)."""

    def __init__(self, objects: dict[str, bytes]):
        self.objects = objects
        self.get_calls = 0

    def get_object(self, Bucket, Key, **kwargs):
        self.get_calls += 1
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "missing"}}, "GetObject")
        etag = f'"etag-{Key}"'
        if kwargs.get("IfNoneMatch") == etag:
            raise ClientError({"Error": {"Code": "304", "Message": "Not Modified"}}, "GetObject")
        body = MagicMock()
        body.read.return_value = self.objects[Key]
        return {"Body": body, "ETag": etag}


def _gz_catalog(market: str, items: list[dict]) -> bytes:
    payload = {
        "market": market,
        "generated_at": "2026-07-31T00:00:00+00:00",
        "count": len(items),
        "items": items,
    }
    return gzip.compress(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def _item(ticker, name, sector=None, price=None, market_cap=None, market="US"):
    return {
        "ticker": ticker,
        "name": name,
        "market": market,
        "sector": sector,
        "industry": None,
        "price": price,
        "currency": "USD",
        "market_cap": market_cap,
    }


_US_ITEMS = [
    _item("AAPL", "Apple Inc.", "Technology", price=230.5, market_cap=3_500_000_000_000),
    _item("MSFT", "Microsoft Corporation", "Technology", price=420.0, market_cap=3_100_000_000_000),
    _item("BRK-B", "Berkshire Hathaway Inc.", "Financial Services", price=470.0, market_cap=1_000_000_000_000),
    _item("XOM", "Exxon Mobil Corporation", "Energy", price=110.0, market_cap=440_000_000_000),
    _item("ZETA", "Zeta Holdings", "Technology", price=None, market_cap=None),
]
_KR_ITEMS = [
    _item("005930.KS", "삼성전자", "Technology", price=79000, market_cap=470_000_000_000, market="KR"),
]
_JP_ITEMS = [
    _item("7203.T", "Toyota Motor Corporation", "Consumer Cyclical", price=2800, market_cap=370_000_000_000, market="JP"),
]


def _default_objects() -> dict[str, bytes]:
    return {
        "catalog/US.json.gz": _gz_catalog("US", _US_ITEMS),
        "catalog/KR.json.gz": _gz_catalog("KR", _KR_ITEMS),
        "catalog/JP.json.gz": _gz_catalog("JP", _JP_ITEMS),
    }


@pytest.fixture()
def api(monkeypatch):
    """api_handler 모듈을 boto3 모킹 상태로 매번 새로 로드한다 (캐시 초기화 목적)."""
    for key, value in _ENV.items():
        monkeypatch.setenv(key, value)
    with patch("boto3.resource", return_value=MagicMock()), \
         patch("boto3.client", return_value=MagicMock()):
        spec = importlib.util.spec_from_file_location("webui_api_handler_under_test", HANDLER_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

    # 인증: 토큰 캐시를 미리 채워 Cognito 호출 없이 통과시킨다
    mod._token_cache["test-token"] = time.time() + 300
    # DynamoDB / ECS 모킹 (실행 생성 경로)
    mod.table = MagicMock()
    mod.table.scan.return_value = {"Items": []}
    mod.ecs = MagicMock()
    mod.ecs.run_task.return_value = {"failures": []}
    mod.s3 = FakeS3(_default_objects())
    return mod


def _event(method, path, query=None, body=None):
    return {
        "requestContext": {"http": {"method": method}},
        "rawPath": path,
        "queryStringParameters": query,
        "headers": {"x-access-token": "test-token"},
        "body": json.dumps(body) if body is not None else None,
    }


def _call(api, method, path, query=None, body=None):
    res = api.handler(_event(method, path, query=query, body=body), None)
    return res["statusCode"], json.loads(res["body"])


def _post_run(api, ticker):
    date = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
    return _call(api, "POST", "/api/runs", body={"ticker": ticker, "analysis_date": date, "depth": 1})


# ---------------- GET /api/catalog ----------------

def test_catalog_requires_valid_market(api):
    status, body = _call(api, "GET", "/api/catalog")
    assert status == 400
    status, body = _call(api, "GET", "/api/catalog", query={"market": "EU"})
    assert status == 400


def test_catalog_search_by_name_and_ticker(api):
    # 종목명 부분 일치 (대소문자 무시)
    status, body = _call(api, "GET", "/api/catalog", query={"market": "US", "q": "apple"})
    assert status == 200
    assert [i["ticker"] for i in body["items"]] == ["AAPL"]
    assert body["total"] == 1
    assert body["page"] == 1
    assert body["page_size"] == 50
    assert body["generated_at"] == "2026-07-31T00:00:00+00:00"
    # 업종 목록은 필터와 무관하게 해당 시장 전체의 distinct 정렬 목록
    assert body["sectors"] == ["Energy", "Financial Services", "Technology"]

    # 티커 부분 일치
    status, body = _call(api, "GET", "/api/catalog", query={"market": "US", "q": "msf"})
    assert status == 200
    assert [i["ticker"] for i in body["items"]] == ["MSFT"]


def test_catalog_sector_filter(api):
    status, body = _call(api, "GET", "/api/catalog", query={"market": "US", "sector": "Energy"})
    assert status == 200
    assert [i["ticker"] for i in body["items"]] == ["XOM"]
    assert body["total"] == 1


def test_catalog_sort_price_desc_nulls_last(api):
    status, body = _call(api, "GET", "/api/catalog",
                         query={"market": "US", "sort": "price", "order": "desc"})
    assert status == 200
    tickers = [i["ticker"] for i in body["items"]]
    assert tickers[:4] == ["BRK-B", "MSFT", "AAPL", "XOM"]
    # price가 null인 종목은 정렬 방향과 무관하게 맨 뒤
    assert tickers[-1] == "ZETA"


def test_catalog_default_sort_is_name_asc(api):
    status, body = _call(api, "GET", "/api/catalog", query={"market": "US"})
    assert status == 200
    names = [i["name"] for i in body["items"]]
    assert names == sorted(names, key=str.lower)


def test_catalog_pagination(api):
    filler = [_item(f"FILL{i:02d}", f"Filler {i:02d} Corp", "Technology", price=i) for i in range(60)]
    api.s3 = FakeS3({"catalog/US.json.gz": _gz_catalog("US", filler)})

    status, body = _call(api, "GET", "/api/catalog", query={"market": "US", "page": "1"})
    assert status == 200
    assert len(body["items"]) == 50
    assert body["total"] == 60

    status, body = _call(api, "GET", "/api/catalog", query={"market": "US", "page": "2"})
    assert status == 200
    assert len(body["items"]) == 10
    assert body["page"] == 2
    assert body["items"][0]["ticker"] == "FILL50"

    status, _ = _call(api, "GET", "/api/catalog", query={"market": "US", "page": "0"})
    assert status == 400


def test_catalog_not_generated_returns_404(api):
    api.s3 = FakeS3({})
    status, body = _call(api, "GET", "/api/catalog", query={"market": "KR"})
    assert status == 404
    assert "카탈로그" in body["error"]


def test_catalog_cached_second_call_skips_s3(api):
    _call(api, "GET", "/api/catalog", query={"market": "US"})
    assert api.s3.get_calls == 1
    status, body = _call(api, "GET", "/api/catalog", query={"market": "US"})
    assert status == 200
    assert body["total"] == len(_US_ITEMS)
    # TTL 이내 두 번째 호출은 S3를 다시 읽지 않는다
    assert api.s3.get_calls == 1


def test_catalog_requires_auth(api):
    event = _event("GET", "/api/catalog", query={"market": "US"})
    event["headers"] = {}
    res = api.handler(event, None)
    assert res["statusCode"] == 401


# ---------------- POST /api/runs 티커 검증 ----------------

def test_run_ticker_exact_match_passes(api):
    status, body = _post_run(api, "aapl")
    assert status == 201
    assert "run_id" in body
    item = api.table.put_item.call_args.kwargs["Item"]
    assert item["ticker"] == "AAPL"
    assert api.ecs.run_task.called


def test_run_ticker_normalized_to_catalog_form(api):
    # brk.b 입력 → 카탈로그의 정규 야후 티커 BRK-B로 실행
    status, _ = _post_run(api, "brk.b")
    assert status == 201
    item = api.table.put_item.call_args.kwargs["Item"]
    assert item["ticker"] == "BRK-B"


def test_run_special_assets_bypass_catalog(api):
    # 암호화폐/지수/선물/외환은 카탈로그 조회 없이 통과
    for ticker in ("BTC-USD", "SOL-USDT", "^GSPC", "GC=F", "EURUSD=X"):
        status, _ = _post_run(api, ticker)
        assert status == 201, ticker
        item = api.table.put_item.call_args.kwargs["Item"]
        assert item["ticker"] == ticker
    assert api.s3.get_calls == 0


def test_run_unknown_ticker_returns_suggestions(api):
    # (a) 티커 접두 일치 후보
    status, body = _post_run(api, "AAP")
    assert status == 400
    assert "카탈로그에 없는" in body["error"]
    assert {"ticker": "AAPL", "name": "Apple Inc."} in body["suggestions"]
    assert len(body["suggestions"]) <= 5
    assert not api.table.put_item.called
    assert not api.ecs.run_task.called

    # (b) 종목명 부분 일치 후보 (대소문자 무시, 다른 시장 카탈로그 포함)
    status, body = _post_run(api, "TOYOTA")
    assert status == 400
    assert any(s["ticker"] == "7203.T" for s in body["suggestions"])


def test_run_fail_open_without_any_catalog(api, capsys):
    # 카탈로그가 하나도 없으면(첫 배치 전) 검증을 건너뛰고 기존 동작 유지
    api.s3 = FakeS3({})
    status, body = _post_run(api, "ZZZZ")
    assert status == 201
    item = api.table.put_item.call_args.kwargs["Item"]
    assert item["ticker"] == "ZZZZ"
    assert "skipping ticker validation" in capsys.readouterr().out
