#!/usr/bin/env python3
# ============================================================
# [모듈 개요] G20 매크로 로컬 검증 서버 — dry-run 덤프 위에서 실제 Lambda 핸들러 + 프론트를 띄운다
#
#   python webui/macro/collect.py --dry-run --force --sources bis,yahoo --dump /tmp/macro_dump.json
#   python webui/macro/devserver.py --load /tmp/macro_dump.json [--port 8765]
#   → http://127.0.0.1:8765/dev-login  (가짜 토큰 4개를 localStorage에 넣고 #/macro 로 이동)
#
# 동작
#  - webui/backend/api_handler.py 를 importlib로 **파일 경로에서 그대로** 로드한다. 로드 중
#    boto3.resource/client 를 가로채서(dynamodb → FakeTable 팩토리, s3 → FakeS3, ecs → 가짜 RunTask,
#    그 외 → 호출 시 예외) AWS 호출이 한 번도 나가지 않게 한다. 로드 후 check_auth/check_admin 을
#    통과 람다로 바꾸고, `_macro_api_instance` 를 덤프에서 복원한 FakeTable 기반 MacroApi 로 교체한다.
#  - `/api/*` 요청은 Lambda Function URL 이벤트(rawPath·rawQueryString·requestContext.http.method·
#    queryStringParameters·headers·body·isBase64Encoded)로 바꿔 `api_handler.handler(event, None)` 에
#    넘기고 statusCode/headers/body 를 그대로 돌려준다.
#  - 나머지 경로는 webui/frontend/ 정적 파일 (`/` → index.html). 캐시는 항상 no-store.
#  - `/dev-login[?next=/index.html#/macro/country/KR]` 은 app.js 의 hasTokens()/showApp()/isAdmin()
#    이 읽는 4개 키(ta_access_token·ta_id_token·ta_refresh_token·ta_token_expires)를 채운다.
#
# 로컬 전용이다. 운영 코드(api_handler/macro_api/collect)는 이 파일을 참조하지 않는다.
# MACRO_DEV_NOAUTH(CONTRACT 10장)는 이 서버가 자기 환경에만 세팅하는 표식이다.
# ============================================================
from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import logging
import mimetypes
import os
import sys
import uuid
from collections import Counter
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import parse_qsl, urlsplit

WEBUI_DIR = Path(__file__).resolve().parents[1]
FRONTEND_DIR = WEBUI_DIR / "frontend"
BACKEND_DIR = WEBUI_DIR / "backend"
if str(WEBUI_DIR) not in sys.path:
    sys.path.insert(0, str(WEBUI_DIR))

from macro.collect import DRY_RUN_BUCKET, load_state  # noqa: E402
from macro.fakeddb import FakeS3, FakeTable  # noqa: E402

logger = logging.getLogger("macro.devserver")

MACRO_TABLE = "macro-dev"
RUNS_TABLE = "runs-dev"
DEFAULT_NEXT = "/index.html#/macro"
# api_handler.py가 임포트 시점에 요구하는 환경변수 (전부 더미)
DEV_ENV: dict[str, str] = {
    "TABLE_NAME": RUNS_TABLE,
    "DATA_BUCKET": DRY_RUN_BUCKET,
    "CLUSTER_ARN": "arn:aws:ecs:local:000000000000:cluster/dev",
    "TASK_DEF": "dev-worker:1",
    "SUBNET_IDS": "subnet-dev",
    "SECURITY_GROUP": "sg-dev",
    "COGNITO_CLIENT_ID": "dev",
    "MACRO_TABLE_NAME": MACRO_TABLE,
    "AWS_DEFAULT_REGION": "ap-northeast-2",
    "MACRO_DEV_NOAUTH": "1",
}
_TEXT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}


# ------------------------------------------------------------------ 가짜 AWS
class _NoAws:
    """호출되면 즉시 실패하는 자리표시자 — devserver에서 AWS 호출이 새는지 드러낸다."""

    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, attr: str) -> Any:
        def _blocked(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError(f"devserver: AWS 호출 차단 ({self._name}.{attr})")

        return _blocked


class _FakeDynamo:
    """boto3.resource('dynamodb') 대역 — Table(name)이 FakeTable을 돌려준다."""

    def __init__(self, tables: dict[str, FakeTable]) -> None:
        self.tables = tables

    def Table(self, name: str) -> FakeTable:  # noqa: N802 - boto3 이름 그대로
        return self.tables.setdefault(name, FakeTable(name=name))


class _FakeEcs:
    """ecs.run_task 대역 — 태스크를 띄우지 않고 가짜 ARN을 돌려준다."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def run_task(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        command = (
            (kwargs.get("overrides") or {}).get("containerOverrides") or [{}]
        )[0].get("command")
        logger.info("가짜 RunTask: %s", command)
        arn = f"arn:aws:ecs:local:000000000000:task/dev/{uuid.uuid4().hex}"
        return {"tasks": [{"taskArn": arn}], "failures": []}

    def stop_task(self, **kwargs: Any) -> dict[str, Any]:
        return {}


def load_api_handler(macro_table: FakeTable, s3: FakeS3) -> ModuleType:
    """api_handler.py를 파일에서 로드하고 인증·테이블·S3·ECS를 페이크로 바꾼다."""
    for key, value in DEV_ENV.items():
        os.environ.setdefault(key, value)
    import boto3

    tables = {MACRO_TABLE: macro_table, RUNS_TABLE: FakeTable(name=RUNS_TABLE)}
    dynamo = _FakeDynamo(tables)
    ecs = _FakeEcs()

    def fake_resource(name: str, **kwargs: Any) -> Any:
        return dynamo if name == "dynamodb" else _NoAws(name)

    def fake_client(name: str, **kwargs: Any) -> Any:
        if name == "s3":
            return s3
        if name == "ecs":
            return ecs
        return _NoAws(name)

    orig_resource, orig_client = boto3.resource, boto3.client
    boto3.resource, boto3.client = fake_resource, fake_client  # type: ignore[assignment]
    try:
        spec = importlib.util.spec_from_file_location("api_handler", BACKEND_DIR / "api_handler.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules["api_handler"] = module
        spec.loader.exec_module(module)
    finally:
        boto3.resource, boto3.client = orig_resource, orig_client  # type: ignore[assignment]

    module.check_auth = lambda event: True  # type: ignore[attr-defined]
    module.check_admin = lambda event: True  # type: ignore[attr-defined]
    module.table = tables[RUNS_TABLE]  # type: ignore[attr-defined]
    module.s3 = s3  # type: ignore[attr-defined]
    module.ecs = ecs  # type: ignore[attr-defined]
    module.dynamodb = dynamo  # type: ignore[attr-defined]
    macro_api = getattr(module, "macro_api", None)
    if macro_api is None:
        logger.warning("webui/backend/macro_api.py 없음 — /api/macro/* 는 503으로 응답합니다")
    else:
        module._macro_api_instance = macro_api.MacroApi(  # type: ignore[attr-defined]
            table=macro_table,
            s3=s3,
            bucket=DRY_RUN_BUCKET,
            run_task=module._run_worker_command,  # type: ignore[attr-defined]
        )
        logger.info("macro_api.MacroApi 를 FakeTable(%s) 기반으로 교체", MACRO_TABLE)
    return module


# ------------------------------------------------------------------ 가짜 토큰 / dev-login
def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def fake_jwt(payload: dict[str, Any]) -> str:
    header = _b64url(json.dumps({"alg": "none", "typ": "JWT"}).encode("utf-8"))
    body = _b64url(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    return f"{header}.{body}.sig"


ACCESS_TOKEN = fake_jwt(
    {
        "token_use": "access",
        "client_id": "dev",
        "cognito:groups": ["admins"],
        "exp": 9999999999,
        "username": "dev",
    }
)
ID_TOKEN = fake_jwt({"token_use": "id", "email": "dev@local", "exp": 9999999999})


def dev_login_html(next_path: str) -> str:
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\"><title>dev-login</title></head>"
        "<body><p>로컬 개발용 로그인 중…</p><script>\n"
        f"localStorage.setItem('ta_access_token', {json.dumps(ACCESS_TOKEN)});\n"
        f"localStorage.setItem('ta_id_token', {json.dumps(ID_TOKEN)});\n"
        "localStorage.setItem('ta_refresh_token', 'dev');\n"
        "localStorage.setItem('ta_token_expires', String(Date.now() + 1e9));\n"
        f"location.replace({json.dumps(next_path)});\n"
        "</script></body></html>"
    )


def safe_next(raw: str | None) -> str:
    """리다이렉트 대상은 같은 오리진의 절대 경로만 허용한다."""
    if not raw or not raw.startswith("/") or raw.startswith("//") or "\\" in raw:
        return DEFAULT_NEXT
    return raw


# ------------------------------------------------------------------ HTTP 핸들러
class DevHandler(BaseHTTPRequestHandler):
    server_version = "macro-devserver/1.0"
    api: ModuleType | None = None  # serve()가 클래스 속성으로 주입

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401 - BaseHTTPRequestHandler 규약
        logger.info("%s " + fmt, self.address_string(), *args)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    # -- 라우팅
    def _dispatch(self, method: str) -> None:
        url = urlsplit(self.path)
        if url.path == "/dev-login":
            query = dict(parse_qsl(url.query, keep_blank_values=True))
            self._send(HTTPStatus.OK, dev_login_html(safe_next(query.get("next"))).encode("utf-8"),
                       "text/html; charset=utf-8")
            return
        if url.path.startswith("/api/"):
            self._api(method, url)
            return
        if method != "GET":
            self._send(HTTPStatus.METHOD_NOT_ALLOWED, b"method not allowed", "text/plain")
            return
        self._static(url.path)

    def _api(self, method: str, url: Any) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8") if length else None
        query = dict(parse_qsl(url.query, keep_blank_values=True))
        event = {
            "version": "2.0",
            "rawPath": url.path,
            "rawQueryString": url.query,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "queryStringParameters": query or None,
            "requestContext": {"http": {"method": method, "path": url.path}},
            "body": body,
            "isBase64Encoded": False,
        }
        api = type(self).api
        if api is None:
            self._send(HTTPStatus.SERVICE_UNAVAILABLE,
                       json.dumps({"error": "api_handler 미로드"}).encode("utf-8"),
                       "application/json; charset=utf-8")
            return
        try:
            resp = api.handler(event, None)
        except Exception as exc:  # noqa: BLE001 - 핸들러 예외를 500으로 보여준다
            logger.exception("api_handler 예외 %s %s", method, url.path)
            resp = {"statusCode": 500, "body": json.dumps({"error": f"devserver: {exc}"})}
        status = int(resp.get("statusCode", 200))
        raw = resp.get("body") or ""
        data = base64.b64decode(raw) if resp.get("isBase64Encoded") else str(raw).encode("utf-8")
        headers = {str(k).lower(): str(v) for k, v in (resp.get("headers") or {}).items()}
        ctype = headers.pop("content-type", "application/json; charset=utf-8")
        self._send(HTTPStatus(status) if status in HTTPStatus._value2member_map_ else status,
                   data, ctype, extra=headers)

    def _static(self, path: str) -> None:
        rel = path.lstrip("/") or "index.html"
        root = FRONTEND_DIR.resolve()
        target = (root / rel).resolve()
        if root not in target.parents and target != root:
            self._send(HTTPStatus.FORBIDDEN, b"forbidden", "text/plain")
            return
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
            return
        ctype = _TEXT_TYPES.get(target.suffix.lower()) or mimetypes.guess_type(target.name)[0] \
            or "application/octet-stream"
        self._send(HTTPStatus.OK, target.read_bytes(), ctype)

    def _send(self, status: Any, data: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(int(status))
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            if k not in ("content-length", "cache-control"):
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)


# ------------------------------------------------------------------ 진입
def load_dump(path: str | None) -> tuple[FakeTable, FakeS3]:
    table = FakeTable(name=MACRO_TABLE)
    s3 = FakeS3()
    if not path:
        logger.warning("--load 없음 — 빈 테이블로 시작합니다 (모든 카드가 비어 보임)")
        return table, s3
    n = load_state(path, table, s3)
    kinds = Counter(str(it.get("pk", "")).split("#", 1)[0] for it in table.items.values())
    logger.info(
        "덤프 복원 %s — 테이블 %d건 (%s) · S3 %d건",
        path,
        n,
        ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())),
        len(s3.objects),
    )
    return table, s3


def serve(host: str, port: int, load: str | None) -> None:
    table, s3 = load_dump(load)
    api = load_api_handler(table, s3)
    DevHandler.api = api
    httpd = ThreadingHTTPServer((host, port), DevHandler)
    logger.info("devserver http://%s:%d/dev-login  (정적: %s)", host, port, FRONTEND_DIR)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="G20 매크로 로컬 검증 서버 (운영 코드와 무관)")
    p.add_argument("--load", default=None, help="collect.py --dump 산출물(JSON)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    serve(args.host, args.port, args.load)
    return 0


if __name__ == "__main__":
    sys.exit(main())
