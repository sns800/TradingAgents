"""실행 취소(cancel) + 동일 종목 중복 방지 테스트.

test_restart_api.py의 FakeS3/모킹 패턴을 따른다.
"""
import gzip
import importlib.util
import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("botocore", reason="webui Lambda deps not installed")

HANDLER_PATH = Path(__file__).resolve().parents[1] / "webui" / "backend" / "api_handler.py"

_ENV = {
    "TABLE_NAME": "t", "DATA_BUCKET": "b", "CLUSTER_ARN": "c",
    "TASK_DEF": "d", "SUBNET_IDS": "s1,s2", "SECURITY_GROUP": "sg",
    "COGNITO_CLIENT_ID": "client", "COGNITO_USER_POOL_ID": "pool",
    "MAX_ACTIVE_RUNS": "10",
}


def _catalog_gz(items):
    payload = {"market": "US", "generated_at": "2026-08-02T00:00:00+00:00",
               "count": len(items), "items": items}
    return gzip.compress(json.dumps(payload).encode("utf-8"))


class FakeS3:
    def __init__(self, objects):
        self._objects = objects

    def get_object(self, Bucket, Key, **kw):
        if Key in self._objects:
            body = MagicMock()
            body.read.return_value = self._objects[Key]
            return {"Body": body, "ETag": '"x"'}
        from botocore.exceptions import ClientError
        raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")


def _objects():
    # AAPL이 카탈로그에 있어 티커 검증 통과
    return {"catalog/US.json.gz": _catalog_gz(
        [{"ticker": "AAPL", "name": "Apple", "market": "US", "sector": None,
          "industry": None, "price": None, "currency": "USD", "market_cap": None}]
    )}


@pytest.fixture
def api(monkeypatch):
    for k, v in _ENV.items():
        monkeypatch.setenv(k, v)
    with patch("boto3.resource", return_value=MagicMock()), \
         patch("boto3.client", return_value=MagicMock()):
        spec = importlib.util.spec_from_file_location("webui_cancel_handler", HANDLER_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    mod._token_cache["tok"] = time.time() + 300
    mod.table = MagicMock()
    mod.table.scan.return_value = {"Items": []}
    # __config__ 조회 기본값: 항목 없음 → env(MAX_ACTIVE_RUNS=10) 폴백.
    # (개별 테스트에서 get_item.return_value를 덮어써 실행 레코드를 준다)
    mod.table.get_item.return_value = {}
    mod.ecs = MagicMock()
    mod.ecs.run_task.return_value = {"failures": [], "tasks": [{"taskArn": "arn:task/abc"}]}
    mod.s3 = FakeS3(_objects())
    return mod


def _event(method, path, body=None):
    return {
        "requestContext": {"http": {"method": method}},
        "rawPath": path, "queryStringParameters": None,
        "headers": {"x-access-token": "tok"},
        "body": json.dumps(body) if body is not None else None,
    }


def _post(api, path, body=None):
    res = api.handler(_event("POST", path, body), None)
    return res["statusCode"], json.loads(res["body"])


# ---------------- 동일 종목 중복 방지 ----------------

def test_duplicate_active_ticker_blocked(api):
    """같은 종목이 진행 중(running)이면 새 실행이 409로 거부되는지 검증."""
    api.table.scan.return_value = {"Items": [{"run_id": "aaa", "ticker": "AAPL"}]}
    status, body = _post(api, "/api/runs",
                         {"ticker": "AAPL", "analysis_date": "2026-07-30", "depth": 1})
    assert status == 409
    assert "진행 중" in body["error"]
    api.ecs.run_task.assert_not_called()


def test_different_ticker_allowed(api):
    """다른 종목이 진행 중이면 새 실행이 허용되는지 검증."""
    api.table.scan.return_value = {"Items": [{"run_id": "bbb", "ticker": "MSFT"}]}
    status, body = _post(api, "/api/runs",
                         {"ticker": "AAPL", "analysis_date": "2026-07-30", "depth": 1})
    assert status == 201
    api.ecs.run_task.assert_called_once()


def test_dedup_case_insensitive(api):
    """대소문자가 달라도 같은 종목으로 보고 차단하는지 검증."""
    api.table.scan.return_value = {"Items": [{"run_id": "ccc", "ticker": "AAPL"}]}
    status, _ = _post(api, "/api/runs",
                      {"ticker": "aapl", "analysis_date": "2026-07-30", "depth": 1})
    assert status == 409


def test_task_arn_saved_on_start(api):
    """실행 시작 시 task_arn이 저장되는지 검증(취소에 필요)."""
    status, _ = _post(api, "/api/runs",
                      {"ticker": "AAPL", "analysis_date": "2026-07-30", "depth": 1})
    assert status == 201
    calls = [c for c in api.table.update_item.call_args_list
             if "task_arn" in str(c)]
    assert calls, "task_arn 저장 update_item 호출이 없음"


# ---------------- 실행 취소 ----------------

def test_cancel_running_stops_task_and_marks_cancelled(api):
    """running 실행 취소 시 태스크 중지 + status=cancelled."""
    api.table.get_item.return_value = {"Item": {
        "run_id": "abc123def456", "status": "running", "task_arn": "arn:task/xyz"}}
    status, body = _post(api, "/api/runs/abc123def456/cancel")
    assert status == 200
    assert body["status"] == "cancelled"
    api.ecs.stop_task.assert_called_once()
    upd = str(api.table.update_item.call_args)
    assert "cancelled" in upd

def test_cancel_finished_run_rejected(api):
    """이미 완료된 실행은 취소 불가(409)."""
    api.table.get_item.return_value = {"Item": {
        "run_id": "abc123def456", "status": "completed"}}
    status, body = _post(api, "/api/runs/abc123def456/cancel")
    assert status == 409
    api.ecs.stop_task.assert_not_called()

def test_cancel_unknown_run_404(api):
    api.table.get_item.return_value = {}
    status, _ = _post(api, "/api/runs/abc123def456/cancel")
    assert status == 404

def test_cancel_without_task_arn_still_marks_cancelled(api):
    """task_arn이 없어도(구형 레코드) 상태는 cancelled로 확정."""
    api.table.get_item.return_value = {"Item": {
        "run_id": "abc123def456", "status": "queued"}}
    status, body = _post(api, "/api/runs/abc123def456/cancel")
    assert status == 200
    assert body["status"] == "cancelled"
    api.ecs.stop_task.assert_not_called()
