# ============================================================
# [모듈 개요] 웹 UI API (Lambda 함수)
#
# CloudFront 뒤의 Lambda Function URL(OAC 보호)로 동작하는 작은 REST API.
#  - POST /api/runs            새 분석 실행 생성 → ECS Fargate 태스크 시작
#  - GET  /api/runs            실행 목록 (최신순 최대 50건)
#  - GET  /api/runs/{id}       실행 상세 + 보고서 파일 목록
#  - GET  /api/runs/{id}/report?name=...  보고서 마크다운 내용
#
# 의존성은 boto3(런타임 내장)뿐이라 별도 패키징 없이 zip 한 장으로 배포됩니다.
# ============================================================
import base64
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import boto3

TABLE_NAME = os.environ["TABLE_NAME"]
DATA_BUCKET = os.environ["DATA_BUCKET"]
CLUSTER_ARN = os.environ["CLUSTER_ARN"]
TASK_DEF = os.environ["TASK_DEF"]
SUBNET_IDS = os.environ["SUBNET_IDS"].split(",")
SECURITY_GROUP = os.environ["SECURITY_GROUP"]
CONTAINER_NAME = os.environ.get("CONTAINER_NAME", "worker")
# 동시 실행 상한: Fargate/Bedrock 비용 폭주 방지용 안전장치
MAX_ACTIVE_RUNS = int(os.environ.get("MAX_ACTIVE_RUNS", "3"))
# Cognito 인증: 이 앱 클라이언트로 발급된 액세스 토큰만 허용
COGNITO_CLIENT_ID = os.environ["COGNITO_CLIENT_ID"]

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)
s3 = boto3.client("s3")
ecs = boto3.client("ecs")
cognito = boto3.client("cognito-idp")

# 검증된 토큰의 짧은 캐시 (토큰 해시 -> 만료 시각). Lambda 컨테이너 재사용 시
# 폴링 요청마다 Cognito를 호출하지 않기 위한 것으로, 5분이면 충분히 짧다.
_token_cache: dict[str, float] = {}
_TOKEN_CACHE_TTL = 300


def _decode_jwt_payload(token: str) -> dict:
    """서명 검증 없이 JWT 페이로드만 디코드한다 (클레임 사전 검사용).

    실제 유효성(서명/폐기 여부)은 Cognito GetUser 호출이 보증하므로,
    여기서는 client_id/token_use 클레임 확인에만 사용한다.
    """
    part = token.split(".")[1]
    part += "=" * (-len(part) % 4)
    return json.loads(base64.urlsafe_b64decode(part))


def check_auth(event) -> bool:
    """x-access-token 헤더의 Cognito 액세스 토큰을 검증한다."""
    headers = event.get("headers") or {}
    token = headers.get("x-access-token", "")
    if not token:
        return False

    now = time.time()
    cached = _token_cache.get(token)
    if cached and cached > now:
        return True

    try:
        claims = _decode_jwt_payload(token)
        if claims.get("token_use") != "access":
            return False
        if claims.get("client_id") != COGNITO_CLIENT_ID:
            return False
        # 서명·폐기 검증은 Cognito에 위임 (유효하지 않으면 예외 발생)
        cognito.get_user(AccessToken=token)
    except Exception:
        return False

    # 토큰 자체 만료와 캐시 TTL 중 이른 쪽까지만 캐시
    _token_cache[token] = min(now + _TOKEN_CACHE_TTL, float(claims.get("exp", 0)))
    if len(_token_cache) > 100:  # 캐시 크기 억제
        _token_cache.clear()
    return True

TICKER_RE = re.compile(r"^[A-Za-z0-9._\-^=]{1,32}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

RUN_FIELDS = (
    "run_id", "ticker", "analysis_date", "depth", "status",
    "decision", "error", "created_at", "updated_at", "started_at", "finished_at",
)


def _plain(value):
    """DynamoDB의 Decimal 등을 JSON 직렬화 가능한 값으로 변환한다."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _resp(status, body):
    return {
        "statusCode": status,
        "headers": {
            "content-type": "application/json; charset=utf-8",
            "cache-control": "no-store",
        },
        "body": json.dumps(body, ensure_ascii=False),
    }


def _err(status, message):
    return _resp(status, {"error": message})


def _run_view(item):
    return {k: _plain(item.get(k)) for k in RUN_FIELDS}


def create_run(body):
    ticker = str(body.get("ticker", "")).strip().upper()
    analysis_date = str(body.get("analysis_date", "")).strip()
    try:
        depth = int(body.get("depth", 1))
    except (TypeError, ValueError):
        return _err(400, "분석 깊이(depth)는 1, 3, 5 중 하나여야 합니다.")

    if not TICKER_RE.match(ticker):
        return _err(400, "올바른 종목 코드를 입력해 주세요 (예: AAPL, 005930.KS, BTC-USD).")
    if not DATE_RE.match(analysis_date):
        return _err(400, "날짜는 YYYY-MM-DD 형식이어야 합니다.")
    try:
        parsed = datetime.strptime(analysis_date, "%Y-%m-%d").date()
    except ValueError:
        return _err(400, "존재하지 않는 날짜입니다.")
    if parsed > datetime.now(timezone.utc).date():
        return _err(400, "분석 날짜는 미래일 수 없습니다.")
    if depth not in (1, 3, 5):
        return _err(400, "분석 깊이(depth)는 1, 3, 5 중 하나여야 합니다.")

    # 동시 실행 수 제한 (테이블이 작아 scan으로 충분)
    active = table.scan(
        ProjectionExpression="run_id",
        FilterExpression="#s IN (:q, :r)",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":q": "queued", ":r": "running"},
    )["Items"]
    if len(active) >= MAX_ACTIVE_RUNS:
        return _err(429, f"동시 실행은 최대 {MAX_ACTIVE_RUNS}건입니다. 진행 중인 분석이 끝난 뒤 다시 시도하세요.")

    run_id = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc).isoformat()
    table.put_item(Item={
        "run_id": run_id,
        "ticker": ticker,
        "analysis_date": analysis_date,
        "depth": depth,
        "status": "queued",
        "created_at": now,
        "updated_at": now,
    })

    try:
        result = ecs.run_task(
            cluster=CLUSTER_ARN,
            taskDefinition=TASK_DEF,
            launchType="FARGATE",
            count=1,
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": SUBNET_IDS,
                    "securityGroups": [SECURITY_GROUP],
                    "assignPublicIp": "ENABLED",
                }
            },
            overrides={
                "containerOverrides": [{
                    "name": CONTAINER_NAME,
                    "environment": [{"name": "RUN_ID", "value": run_id}],
                }]
            },
        )
        failures = result.get("failures") or []
        if failures:
            raise RuntimeError(failures[0].get("reason", "RunTask failed"))
    except Exception as e:
        table.update_item(
            Key={"run_id": run_id},
            UpdateExpression="SET #s = :s, #e = :e, updated_at = :u",
            ExpressionAttributeNames={"#s": "status", "#e": "error"},
            ExpressionAttributeValues={
                ":s": "failed",
                ":e": f"분석 태스크 시작 실패: {e}"[:500],
                ":u": datetime.now(timezone.utc).isoformat(),
            },
        )
        return _err(500, f"분석 태스크를 시작하지 못했습니다: {e}")

    return _resp(201, {"run_id": run_id})


def list_runs():
    items = table.scan().get("Items", [])
    items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return _resp(200, {"runs": [_run_view(i) for i in items[:50]]})


def get_run(run_id):
    item = table.get_item(Key={"run_id": run_id}).get("Item")
    if not item:
        return _err(404, "해당 실행을 찾을 수 없습니다.")
    reports = [str(r) for r in item.get("reports", [])]
    return _resp(200, {"run": _run_view(item), "reports": reports})


def get_report(run_id, name):
    item = table.get_item(Key={"run_id": run_id}).get("Item")
    if not item:
        return _err(404, "해당 실행을 찾을 수 없습니다.")
    reports = [str(r) for r in item.get("reports", [])]
    if name not in reports:
        return _err(404, "해당 보고서 파일이 없습니다.")
    obj = s3.get_object(Bucket=DATA_BUCKET, Key=f"runs/{run_id}/reports/{name}")
    content = obj["Body"].read().decode("utf-8")
    return _resp(200, {"name": name, "content": content})


def handler(event, _context):
    http = event.get("requestContext", {}).get("http", {})
    method = http.get("method", "GET")
    path = event.get("rawPath", "/")
    query = event.get("queryStringParameters") or {}

    if not check_auth(event):
        return _err(401, "로그인이 필요합니다.")

    try:
        if method == "POST" and path == "/api/runs":
            try:
                raw = event.get("body") or "{}"
                if event.get("isBase64Encoded"):
                    import base64
                    raw = base64.b64decode(raw).decode("utf-8")
                body = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                return _err(400, "요청 본문이 올바른 JSON이 아닙니다.")
            return create_run(body)

        if method == "GET" and path == "/api/runs":
            return list_runs()

        m = re.match(r"^/api/runs/([a-f0-9]{12})$", path)
        if method == "GET" and m:
            return get_run(m.group(1))

        m = re.match(r"^/api/runs/([a-f0-9]{12})/report$", path)
        if method == "GET" and m:
            name = (query.get("name") or "").strip()
            if not name:
                return _err(400, "name 쿼리 파라미터가 필요합니다.")
            return get_report(m.group(1), name)

        return _err(404, "존재하지 않는 API 경로입니다.")
    except Exception as e:
        print(f"unhandled error: {e}")
        return _err(500, "서버 내부 오류가 발생했습니다.")
