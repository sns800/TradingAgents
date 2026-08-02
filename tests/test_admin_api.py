# ============================================================
# [테스트 개요] 웹 UI 관리자 API (Cognito 계정 관리 + 동시 한도 설정)
#
# webui/backend/api_handler.py 를 boto3 모킹 상태로 로드해 검증한다.
#  - admin 게이트: cognito:groups에 admins가 없으면 403
#  - GET/POST/DELETE /api/admin/users: 목록·생성·삭제(자기 자신·마지막 admin 방지)
#  - 관리자 토글: 마지막 admin 권한 해제 방지
#  - GET/POST /api/admin/config: 조회·저장(1~50 범위 검증)
#  - __config__ 항목이 GET /api/runs 목록에서 제외되는지
#
# test_catalog_api.py 의 FakeS3/모킹 패턴을 따른다.
# ============================================================
import base64
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
    "COGNITO_USER_POOL_ID": "ap-northeast-2_test",
}

# 요청자(관리자) Cognito Username
_ADMIN_USERNAME = "admin-user"


def _make_token(claims: dict) -> str:
    """서명 검증 없이 페이로드만 디코드되는 JWT 형태의 토큰을 만든다.

    (실제 서명·폐기 검증은 _token_cache 사전 주입으로 우회 — check_auth가
    캐시 히트 시 Cognito 호출 없이 통과하므로 클레임만 있으면 충분하다.)
    """
    def _b64(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return _b64({"alg": "none"}) + "." + _b64(claims) + ".sig"


_ADMIN_TOKEN = _make_token({
    "token_use": "access", "client_id": "client-id",
    "username": _ADMIN_USERNAME, "cognito:groups": ["admins"],
})
_USER_TOKEN = _make_token({
    "token_use": "access", "client_id": "client-id",
    "username": "plain-user", "cognito:groups": [],
})


@pytest.fixture()
def api(monkeypatch):
    """api_handler 모듈을 boto3 모킹 상태로 매번 새로 로드한다."""
    for key, value in _ENV.items():
        monkeypatch.setenv(key, value)
    with patch("boto3.resource", return_value=MagicMock()), \
         patch("boto3.client", return_value=MagicMock()):
        spec = importlib.util.spec_from_file_location("webui_admin_handler_under_test", HANDLER_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

    # 인증: 두 토큰을 캐시에 주입해 Cognito GetUser 없이 check_auth 통과
    mod._token_cache[_ADMIN_TOKEN] = time.time() + 300
    mod._token_cache[_USER_TOKEN] = time.time() + 300

    mod.table = MagicMock()
    mod.table.scan.return_value = {"Items": []}
    mod.table.get_item.return_value = {}
    mod.cognito = MagicMock()
    return mod


def _event(method, path, token=_ADMIN_TOKEN, body=None):
    return {
        "requestContext": {"http": {"method": method}},
        "rawPath": path,
        "queryStringParameters": None,
        "headers": {"x-access-token": token} if token else {},
        "body": json.dumps(body) if body is not None else None,
    }


def _call(api, method, path, token=_ADMIN_TOKEN, body=None):
    res = api.handler(_event(method, path, token=token, body=body), None)
    return res["statusCode"], json.loads(res["body"])


# ---------------- admin 게이트 ----------------

def test_admin_endpoints_reject_non_admin(api):
    for method, path in [
        ("GET", "/api/admin/users"),
        ("POST", "/api/admin/users"),
        ("GET", "/api/admin/config"),
        ("POST", "/api/admin/config"),
        ("DELETE", "/api/admin/users/someone"),
    ]:
        status, body = _call(api, method, path, token=_USER_TOKEN, body={} if method == "POST" else None)
        assert status == 403, (method, path)
        assert "관리자" in body["error"]


def test_admin_endpoints_require_auth(api):
    status, _ = _call(api, "GET", "/api/admin/users", token=None)
    assert status == 401


# ---------------- 사용자 목록 ----------------

def test_list_users_marks_admins(api):
    api.cognito.list_users_in_group.return_value = {"Users": [{"Username": _ADMIN_USERNAME}]}
    api.cognito.list_users.return_value = {
        "Users": [
            {"Username": _ADMIN_USERNAME, "UserStatus": "CONFIRMED", "Enabled": True,
             "Attributes": [{"Name": "email", "Value": "admin@example.com"}]},
            {"Username": "plain-user", "UserStatus": "CONFIRMED", "Enabled": True,
             "Attributes": [{"Name": "email", "Value": "user@example.com"}]},
        ]
    }
    status, body = _call(api, "GET", "/api/admin/users")
    assert status == 200
    by_email = {u["email"]: u for u in body["users"]}
    assert by_email["admin@example.com"]["is_admin"] is True
    assert by_email["user@example.com"]["is_admin"] is False


# ---------------- 사용자 생성 ----------------

def test_create_user_sets_permanent_password(api):
    api.cognito.admin_create_user.return_value = {"User": {"Username": "new@example.com"}}
    status, body = _call(api, "POST", "/api/admin/users",
                         body={"email": "new@example.com", "password": "sup3rsecret"})
    assert status == 201
    assert body["is_admin"] is False
    assert api.cognito.admin_create_user.call_args.kwargs["MessageAction"] == "SUPPRESS"
    assert api.cognito.admin_set_user_password.call_args.kwargs["Permanent"] is True
    assert not api.cognito.admin_add_user_to_group.called


def test_create_admin_user_adds_to_group(api):
    api.cognito.admin_create_user.return_value = {"User": {"Username": "boss@example.com"}}
    status, body = _call(api, "POST", "/api/admin/users",
                         body={"email": "boss@example.com", "password": "sup3rsecret", "is_admin": True})
    assert status == 201
    assert body["is_admin"] is True
    assert api.cognito.admin_add_user_to_group.call_args.kwargs["GroupName"] == "admins"


def test_create_user_validates_input(api):
    status, _ = _call(api, "POST", "/api/admin/users",
                      body={"email": "not-an-email", "password": "sup3rsecret"})
    assert status == 400
    status, _ = _call(api, "POST", "/api/admin/users",
                      body={"email": "ok@example.com", "password": "short"})
    assert status == 400
    assert not api.cognito.admin_create_user.called


def test_create_user_duplicate_returns_409(api):
    api.cognito.admin_create_user.side_effect = ClientError(
        {"Error": {"Code": "UsernameExistsException", "Message": "exists"}}, "AdminCreateUser")
    status, body = _call(api, "POST", "/api/admin/users",
                         body={"email": "dup@example.com", "password": "sup3rsecret"})
    assert status == 409
    assert "이미 존재" in body["error"]


# ---------------- 관리자 토글 ----------------

def test_set_admin_add(api):
    status, body = _call(api, "POST", "/api/admin/users/u2/admin", body={"is_admin": True})
    assert status == 200
    assert body["is_admin"] is True
    assert api.cognito.admin_add_user_to_group.called


def test_set_admin_remove_last_admin_blocked(api):
    api.cognito.list_users_in_group.return_value = {"Users": [{"Username": "solo-admin"}]}
    status, body = _call(api, "POST", "/api/admin/users/solo-admin/admin", body={"is_admin": False})
    assert status == 400
    assert "마지막 관리자" in body["error"]
    assert not api.cognito.admin_remove_user_from_group.called


# ---------------- 사용자 삭제 ----------------

def test_delete_self_blocked(api):
    # 요청자와 동일한 username 삭제 시도 → 방지
    status, body = _call(api, "DELETE", f"/api/admin/users/{_ADMIN_USERNAME}")
    assert status == 400
    assert "자기 자신" in body["error"]
    assert not api.cognito.admin_delete_user.called


def test_delete_last_admin_blocked(api):
    api.cognito.list_users_in_group.return_value = {"Users": [{"Username": "solo-admin"}]}
    status, body = _call(api, "DELETE", "/api/admin/users/solo-admin")
    assert status == 400
    assert "마지막 관리자" in body["error"]
    assert not api.cognito.admin_delete_user.called


def test_delete_user_success(api):
    api.cognito.list_users_in_group.return_value = {
        "Users": [{"Username": _ADMIN_USERNAME}, {"Username": "other-admin"}]}
    status, body = _call(api, "DELETE", "/api/admin/users/plain-user")
    assert status == 200
    assert api.cognito.admin_delete_user.call_args.kwargs["Username"] == "plain-user"


def test_delete_user_url_encoded(api):
    api.cognito.list_users_in_group.return_value = {"Users": []}
    status, _ = _call(api, "DELETE", "/api/admin/users/foo%40example.com")
    assert status == 200
    assert api.cognito.admin_delete_user.call_args.kwargs["Username"] == "foo@example.com"


# ---------------- 동시 한도 설정 ----------------

def test_get_config_falls_back_to_env(api):
    # __config__ 항목 없음 → env var(MAX_ACTIVE_RUNS 미설정) 기본값 3
    api.table.get_item.return_value = {}
    status, body = _call(api, "GET", "/api/admin/config")
    assert status == 200
    assert body["max_active_runs"] == 3


def test_get_config_uses_stored_value(api):
    api.table.get_item.return_value = {"Item": {"run_id": "__config__", "max_active_runs": 25}}
    status, body = _call(api, "GET", "/api/admin/config")
    assert status == 200
    assert body["max_active_runs"] == 25


def test_set_config_saves_value(api):
    status, body = _call(api, "POST", "/api/admin/config", body={"max_active_runs": 20})
    assert status == 200
    assert body["max_active_runs"] == 20
    key = api.table.update_item.call_args.kwargs["Key"]
    assert key == {"run_id": "__config__"}


def test_set_config_range_validation(api):
    for bad in (0, 51, -1, "x", None):
        status, body = _call(api, "POST", "/api/admin/config", body={"max_active_runs": bad})
        assert status == 400, bad
        assert "1~50" in body["error"]
    assert not api.table.update_item.called


# ---------------- __config__ 항목이 실행 목록에서 제외 ----------------

def test_config_item_excluded_from_list_runs(api):
    api.table.scan.return_value = {"Items": [
        {"run_id": "__config__", "max_active_runs": 20},
        {"run_id": "abc123def456", "ticker": "AAPL", "status": "completed",
         "created_at": "2026-07-30T00:00:00+00:00"},
    ]}
    status, body = _call(api, "GET", "/api/runs", token=_USER_TOKEN)
    assert status == 200
    ids = [r["run_id"] for r in body["runs"]]
    assert "__config__" not in ids
    assert "abc123def456" in ids
