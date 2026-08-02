# ============================================================
# [테스트 개요] 웹 UI 기존 분석 재시작 API
#
# webui/backend/api_handler.py 를 boto3 모킹 상태로 로드해
# POST /api/runs/{run_id}/restart 를 검증한다.
#  - 원본 종목·날짜 복사 + 새 depth 적용, restarted_from 기록
#  - 없는 run_id 404, 잘못된 depth 400
#  - create_run과 동시성 제한(_start_run) 재사용 확인 (429)
#
# test_catalog_api.py 의 FakeS3/모킹 패턴을 따른다.
# ============================================================
import gzip
import importlib.util
import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# botocore는 웹 UI Lambda 전용 의존성이라 순수 dev 설치(CI)에는 없다.
pytest.importorskip("botocore", reason="webui Lambda deps not installed")
from botocore.exceptions import ClientError  # noqa: E402

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

# 재시작 원본이 되는 실행 레코드 (실패·얕게 돌린 실행 가정)
_ORIGINAL_ID = "abc123def456"
_ORIGINAL_RUN = {
    "run_id": _ORIGINAL_ID,
    "ticker": "AAPL",
    "analysis_date": "2026-07-30",
    "depth": 1,
    "status": "failed",
    "error": "일시적 오류",
}


class FakeS3:
    """catalog/*.json.gz 를 흉내 내는 최소 S3 클라이언트."""

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
    payload = {"market": market, "generated_at": "2026-07-31T00:00:00+00:00",
               "count": len(items), "items": items}
    return gzip.compress(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def _default_objects() -> dict[str, bytes]:
    aapl = {
        "ticker": "AAPL", "name": "Apple Inc.", "market": "US",
        "sector": "Technology", "industry": None, "price": 230.5,
        "currency": "USD", "market_cap": 3_500_000_000_000,
    }
    return {"catalog/US.json.gz": _gz_catalog("US", [aapl])}


@pytest.fixture()
def api(monkeypatch):
    """api_handler 모듈을 boto3 모킹 상태로 매번 새로 로드한다."""
    for key, value in _ENV.items():
        monkeypatch.setenv(key, value)
    with patch("boto3.resource", return_value=MagicMock()), \
         patch("boto3.client", return_value=MagicMock()):
        spec = importlib.util.spec_from_file_location("webui_restart_handler_under_test", HANDLER_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

    # 인증: 토큰 캐시를 미리 채워 Cognito 호출 없이 통과시킨다
    mod._token_cache["test-token"] = time.time() + 300
    # DynamoDB / ECS 모킹
    mod.table = MagicMock()
    mod.table.scan.return_value = {"Items": []}
    mod.table.get_item.return_value = {"Item": dict(_ORIGINAL_RUN)}
    mod.ecs = MagicMock()
    mod.ecs.run_task.return_value = {"failures": []}
    mod.s3 = FakeS3(_default_objects())
    return mod


def _event(method, path, body=None):
    return {
        "requestContext": {"http": {"method": method}},
        "rawPath": path,
        "queryStringParameters": None,
        "headers": {"x-access-token": "test-token"},
        "body": json.dumps(body) if body is not None else None,
    }


def _restart(api, run_id, depth):
    body = {"depth": depth} if depth is not None else {}
    res = api.handler(_event("POST", f"/api/runs/{run_id}/restart", body=body), None)
    return res["statusCode"], json.loads(res["body"])


# ---------------- POST /api/runs/{id}/restart ----------------

def test_restart_copies_ticker_date_and_applies_new_depth(api):
    status, body = _restart(api, _ORIGINAL_ID, 5)
    assert status == 201
    assert "run_id" in body
    # 새 실행은 다른 run_id를 갖는다
    assert body["run_id"] != _ORIGINAL_ID

    item = api.table.put_item.call_args.kwargs["Item"]
    # 원본 종목·날짜 복사, depth는 요청값(5)
    assert item["ticker"] == "AAPL"
    assert item["analysis_date"] == "2026-07-30"
    assert item["depth"] == 5
    assert item["status"] == "queued"
    # RunTask가 새 run_id로 트리거된다
    assert api.ecs.run_task.called
    env = api.ecs.run_task.call_args.kwargs["overrides"]["containerOverrides"][0]["environment"]
    assert {"name": "RUN_ID", "value": item["run_id"]} in env


def test_restart_records_restarted_from(api):
    status, body = _restart(api, _ORIGINAL_ID, 3)
    assert status == 201
    item = api.table.put_item.call_args.kwargs["Item"]
    assert item["restarted_from"] == _ORIGINAL_ID


def test_restart_unknown_run_returns_404(api):
    api.table.get_item.return_value = {}  # 원본 없음
    status, body = _restart(api, "ffffffffffff", 3)
    assert status == 404
    assert "찾을 수 없" in body["error"]
    assert not api.table.put_item.called
    assert not api.ecs.run_task.called


def test_restart_invalid_depth_returns_400(api):
    for bad in (2, 4, 0, "x"):
        status, body = _restart(api, _ORIGINAL_ID, bad)
        assert status == 400, bad
        assert "깊이" in body["error"]
    # depth 누락도 400
    status, body = _restart(api, _ORIGINAL_ID, None)
    assert status == 400
    assert not api.table.put_item.called
    assert not api.ecs.run_task.called


def test_restart_reuses_concurrency_limit(api):
    # 동시 실행 상한(_start_run 재사용)에 걸리면 429
    api.table.scan.return_value = {"Items": [{"run_id": f"r{i}"} for i in range(3)]}
    status, body = _restart(api, _ORIGINAL_ID, 5)
    assert status == 429
    assert "동시 실행" in body["error"]
    assert not api.table.put_item.called
    assert not api.ecs.run_task.called


def test_restart_requires_auth(api):
    event = _event("POST", f"/api/runs/{_ORIGINAL_ID}/restart", body={"depth": 3})
    event["headers"] = {}
    res = api.handler(event, None)
    assert res["statusCode"] == 401
