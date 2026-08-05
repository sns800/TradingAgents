"""실행 삭제(DELETE /api/runs/{id}) 테스트.

test_cancel_and_dedup.py의 모킹 패턴을 따른다. S3는 list_objects_v2 /
delete_objects가 필요하므로 MagicMock을 사용한다.
"""
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

RUN_ID = "abc123def456"


@pytest.fixture
def api(monkeypatch):
    for k, v in _ENV.items():
        monkeypatch.setenv(k, v)
    with patch("boto3.resource", return_value=MagicMock()), \
         patch("boto3.client", return_value=MagicMock()):
        spec = importlib.util.spec_from_file_location("webui_delete_handler", HANDLER_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    mod._token_cache["tok"] = time.time() + 300
    mod.table = MagicMock()
    mod.s3 = MagicMock()
    mod.s3.list_objects_v2.return_value = {"Contents": [], "IsTruncated": False}
    mod.ecs = MagicMock()
    return mod


def _delete(api, run_id=RUN_ID):
    event = {
        "requestContext": {"http": {"method": "DELETE"}},
        "rawPath": f"/api/runs/{run_id}", "queryStringParameters": None,
        "headers": {"x-access-token": "tok"}, "body": None,
    }
    res = api.handler(event, None)
    return res["statusCode"], json.loads(res["body"])


def test_delete_completed_removes_record_and_reports(api):
    """완료 실행 삭제: S3 보고서 일괄 삭제 + DynamoDB 레코드 삭제."""
    api.table.get_item.return_value = {"Item": {"run_id": RUN_ID, "status": "completed"}}
    api.s3.list_objects_v2.return_value = {
        "Contents": [{"Key": f"runs/{RUN_ID}/reports/a.md"},
                     {"Key": f"runs/{RUN_ID}/reports/b.md"}],
        "IsTruncated": False,
    }
    status, body = _delete(api)
    assert status == 200 and body["deleted"] is True
    api.s3.delete_objects.assert_called_once()
    deleted_keys = api.s3.delete_objects.call_args.kwargs["Delete"]["Objects"]
    assert {"Key": f"runs/{RUN_ID}/reports/a.md"} in deleted_keys
    api.table.delete_item.assert_called_once_with(Key={"run_id": RUN_ID})


def test_delete_failed_and_cancelled_allowed(api):
    """failed/cancelled 상태도 삭제 가능."""
    for st in ("failed", "cancelled"):
        api.table.reset_mock()
        api.table.get_item.return_value = {"Item": {"run_id": RUN_ID, "status": st}}
        status, _ = _delete(api)
        assert status == 200
        api.table.delete_item.assert_called_once()


def test_delete_active_run_rejected(api):
    """진행 중(queued/running) 실행은 409 — 먼저 취소해야 함."""
    for st in ("queued", "running"):
        api.table.reset_mock()
        api.s3.reset_mock()
        api.table.get_item.return_value = {"Item": {"run_id": RUN_ID, "status": st}}
        status, body = _delete(api)
        assert status == 409
        assert "취소" in body["error"]
        api.table.delete_item.assert_not_called()
        api.s3.delete_objects.assert_not_called()


def test_delete_unknown_run_404(api):
    api.table.get_item.return_value = {}
    status, _ = _delete(api)
    assert status == 404
    api.table.delete_item.assert_not_called()


def test_delete_no_reports_still_deletes_record(api):
    """S3에 보고서가 없어도(빈 prefix) 레코드는 삭제된다."""
    api.table.get_item.return_value = {"Item": {"run_id": RUN_ID, "status": "completed"}}
    status, _ = _delete(api)
    assert status == 200
    api.s3.delete_objects.assert_not_called()
    api.table.delete_item.assert_called_once()


def test_delete_paginates_s3_listing(api):
    """S3 목록이 잘려 있으면(IsTruncated) 이어서 지운다."""
    api.table.get_item.return_value = {"Item": {"run_id": RUN_ID, "status": "completed"}}
    api.s3.list_objects_v2.side_effect = [
        {"Contents": [{"Key": f"runs/{RUN_ID}/reports/a.md"}],
         "IsTruncated": True, "NextContinuationToken": "tok2"},
        {"Contents": [{"Key": f"runs/{RUN_ID}/reports/b.md"}], "IsTruncated": False},
    ]
    status, _ = _delete(api)
    assert status == 200
    assert api.s3.delete_objects.call_count == 2
    assert api.s3.list_objects_v2.call_args_list[1].kwargs["ContinuationToken"] == "tok2"


def test_delete_s3_failure_keeps_record(api):
    """S3 삭제 실패 시 레코드는 지우지 않는다(재시도 가능하게)."""
    api.table.get_item.return_value = {"Item": {"run_id": RUN_ID, "status": "completed"}}
    api.s3.list_objects_v2.return_value = {
        "Contents": [{"Key": f"runs/{RUN_ID}/reports/a.md"}], "IsTruncated": False}
    api.s3.delete_objects.side_effect = RuntimeError("s3 down")
    status, _ = _delete(api)
    assert status == 500
    api.table.delete_item.assert_not_called()


def test_delete_requires_auth(api):
    """토큰 없이 DELETE 호출 시 401."""
    event = {
        "requestContext": {"http": {"method": "DELETE"}},
        "rawPath": f"/api/runs/{RUN_ID}", "queryStringParameters": None,
        "headers": {}, "body": None,
    }
    res = api.handler(event, None)
    assert res["statusCode"] == 401
    api.table.delete_item.assert_not_called()
