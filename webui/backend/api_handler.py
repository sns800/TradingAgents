# ============================================================
# [모듈 개요] 웹 UI API (Lambda 함수)
#
# CloudFront 뒤의 Lambda Function URL(OAC 보호)로 동작하는 작은 REST API.
#  - POST /api/runs            새 분석 실행 생성 → ECS Fargate 태스크 시작
#  - GET  /api/runs            실행 목록 (최신순 최대 50건)
#  - GET  /api/runs/{id}       실행 상세 + 보고서 파일 목록
#  - GET  /api/runs/{id}/report?name=...  보고서 마크다운 내용
#  - GET  /api/catalog         종목 카탈로그 조회 (검색/업종/정렬/페이지네이션)
#
# 의존성은 boto3(런타임 내장)뿐이라 별도 패키징 없이 zip 한 장으로 배포됩니다.
# ============================================================
import base64
import gzip
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

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

# ---------------- 종목 카탈로그 (S3 → 컨테이너 전역 캐시) ----------------
# 다른 워커가 catalog/{US|KR|JP}.json.gz 로 생성해 둔 시장별 종목 목록.
# 수천 종목짜리 JSON이라 매 요청 S3를 읽지 않도록 Lambda 컨테이너 전역에
# 시장별 (데이터, S3 ETag, 로드 시각)을 캐시하고 TTL이 지나면 재로드한다.
CATALOG_MARKETS = ("US", "KR", "JP")
CATALOG_PAGE_SIZE = 50
_catalog_cache: dict[str, dict] = {}
_CATALOG_CACHE_TTL = 600  # 10분

# 카탈로그 없이 통과시키는 야후 특수자산의 암호화폐 호가 통화 접미사
_CRYPTO_SUFFIXES = ("-USD", "-USDT", "-USDC", "-KRW", "-EUR", "-BTC", "-ETH")


def _load_catalog(market: str):
    """시장별 카탈로그를 S3에서 읽어 캐시한다. 파일이 없으면 None.

    TTL 이내면 S3를 건드리지 않고 캐시를 그대로 반환하고, TTL이 지나면
    저장해 둔 ETag로 조건부 GET(IfNoneMatch)을 보내 변경이 없을 때는
    본문 재다운로드 없이 TTL만 연장한다.
    """
    now = time.time()
    cached = _catalog_cache.get(market)
    if cached and now - cached["loaded_at"] < _CATALOG_CACHE_TTL:
        return cached["data"]

    key = f"catalog/{market}.json.gz"
    kwargs = {"Bucket": DATA_BUCKET, "Key": key}
    if cached and cached.get("etag"):
        kwargs["IfNoneMatch"] = cached["etag"]
    try:
        obj = s3.get_object(**kwargs)
        data = json.loads(gzip.decompress(obj["Body"].read()).decode("utf-8"))
    except ClientError as e:
        code = str(e.response.get("Error", {}).get("Code", ""))
        if code in ("304", "NotModified") and cached:
            # 파일이 그대로면 캐시 유지, TTL만 갱신
            cached["loaded_at"] = now
            return cached["data"]
        if code in ("NoSuchKey", "404"):
            return None
        print(f"catalog load failed ({market}): {e}")
        return cached["data"] if cached else None
    except Exception as e:
        # S3 장애 등: 만료된 캐시라도 있으면 그것으로 버틴다
        print(f"catalog load failed ({market}): {e}")
        return cached["data"] if cached else None

    _catalog_cache[market] = {"data": data, "etag": obj.get("ETag"), "loaded_at": now}
    return data


def _is_special_asset(ticker: str) -> bool:
    """카탈로그에 없어도 통과시키는 야후 특수자산 패턴 화이트리스트.

    암호화폐(-USD 등 호가 통화 접미사), 선물(=F), 외환(=X), 지수(^) 표기.
    """
    if ticker.startswith("^"):
        return True
    if ticker.endswith(("=F", "=X")):
        return True
    return ticker.endswith(_CRYPTO_SUFFIXES)


def _validate_ticker(ticker: str):
    """카탈로그 기반 티커 검증. (정규 티커, 오류 응답) 튜플을 반환한다.

    - 특수자산 패턴이면 카탈로그 조회 없이 통과
    - 3개 시장 카탈로그에서 정확 일치하면 카탈로그의 정규 티커로 교체
      ('.'을 '-'로 바꾼 변형도 함께 대조: brk.b → BRK-B)
    - 카탈로그가 하나도 로드되지 않으면 검증을 건너뛴다 (fail-open)
    - 그 외에는 400 + 후보 제안 (티커 접두 일치 → 종목명 부분 일치 순)
    """
    if _is_special_asset(ticker):
        return ticker, None

    catalogs = [_load_catalog(m) for m in CATALOG_MARKETS]
    loaded = [c for c in catalogs if c]
    if not loaded:
        # 첫 배치 전이거나 S3 오류: 기존 동작대로 통과시킨다
        print(f"warning: no catalog loaded, skipping ticker validation for {ticker}")
        return ticker, None

    # 정확 일치 검색 (입력은 이미 대문자 정규화된 상태)
    exact_keys = {ticker, ticker.replace(".", "-")}
    for data in loaded:
        for item in data.get("items", []):
            if str(item.get("ticker") or "").upper() in exact_keys:
                return str(item.get("ticker")), None

    # 후보 제안: (a) 티커 접두 일치 → (b) 종목명 부분 일치, 최대 5건
    suggestions: list[dict] = []
    seen: set[str] = set()

    def _collect(match_fn):
        for data in loaded:
            for item in data.get("items", []):
                if len(suggestions) >= 5:
                    return
                t = str(item.get("ticker") or "")
                if t.upper() in seen or not match_fn(item):
                    continue
                seen.add(t.upper())
                suggestions.append({"ticker": t, "name": item.get("name")})

    needle = ticker.lower()
    _collect(lambda i: str(i.get("ticker") or "").upper().startswith(ticker))
    _collect(lambda i: needle in str(i.get("name") or "").lower())

    return None, _resp(400, {
        "error": "카탈로그에 없는 티커입니다. 종목 코드를 확인해 주세요.",
        "suggestions": suggestions,
    })

RUN_FIELDS = (
    "run_id", "ticker", "analysis_date", "depth", "status",
    "decision", "error", "created_at", "updated_at", "started_at", "finished_at",
    "restarted_from",
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


def get_catalog(query):
    """종목 카탈로그 조회: 검색(q)·업종(sector) 필터, 정렬, 50건 페이지네이션."""
    market = str(query.get("market") or "").strip().upper()
    if market not in CATALOG_MARKETS:
        return _err(400, "market 파라미터는 US, KR, JP 중 하나여야 합니다.")

    sort = str(query.get("sort") or "name").strip().lower()
    if sort not in ("name", "price", "market_cap"):
        return _err(400, "sort는 name, price, market_cap 중 하나여야 합니다.")
    order = str(query.get("order") or "asc").strip().lower()
    if order not in ("asc", "desc"):
        return _err(400, "order는 asc 또는 desc여야 합니다.")
    try:
        page = int(query.get("page") or 1)
    except (TypeError, ValueError):
        return _err(400, "page는 1 이상의 정수여야 합니다.")
    if page < 1:
        return _err(400, "page는 1 이상의 정수여야 합니다.")

    data = _load_catalog(market)
    if data is None:
        return _err(404, "카탈로그가 아직 생성되지 않았습니다. 잠시 후 다시 시도해 주세요.")

    items = list(data.get("items", []))
    # 업종 목록은 필터 적용 전, 해당 시장 전체 기준
    sectors = sorted({str(i["sector"]) for i in items if i.get("sector")})

    q = str(query.get("q") or "").strip().lower()
    if q:
        items = [
            i for i in items
            if q in str(i.get("ticker") or "").lower() or q in str(i.get("name") or "").lower()
        ]
    sector = str(query.get("sector") or "").strip()
    if sector:
        items = [i for i in items if i.get("sector") == sector]

    reverse = order == "desc"
    if sort == "name":
        items.sort(key=lambda i: str(i.get("name") or i.get("ticker") or "").lower(), reverse=reverse)
    else:
        # 숫자 정렬: 값이 없는(null) 종목은 정렬 방향과 무관하게 항상 뒤로
        present = [i for i in items if i.get(sort) is not None]
        missing = [i for i in items if i.get(sort) is None]
        present.sort(key=lambda i: i[sort], reverse=reverse)
        items = present + missing

    total = len(items)
    start = (page - 1) * CATALOG_PAGE_SIZE
    return _resp(200, {
        "items": items[start:start + CATALOG_PAGE_SIZE],
        "total": total,
        "page": page,
        "page_size": CATALOG_PAGE_SIZE,
        "generated_at": data.get("generated_at"),
        "sectors": sectors,
    })


def _parse_depth(raw):
    """분석 깊이(depth)를 검증한다. (depth, 오류 응답) 튜플을 반환한다."""
    try:
        depth = int(raw)
    except (TypeError, ValueError):
        return None, _err(400, "분석 깊이(depth)는 1, 3, 5 중 하나여야 합니다.")
    if depth not in (1, 3, 5):
        return None, _err(400, "분석 깊이(depth)는 1, 3, 5 중 하나여야 합니다.")
    return depth, None


def _start_run(ticker, analysis_date, depth, extra=None):
    """티커 검증·동시성 제한·RunTask 트리거를 처리하고 새 실행을 생성한다.

    create_run과 restart_run이 공유하는 핵심 로직. ticker/analysis_date/depth는
    이미 형식 검증을 마친 값이어야 하며, 여기서는 카탈로그 대조로 정규 티커를
    확정하고 동시 실행 상한을 확인한 뒤 Fargate 태스크를 띄운다.
    extra는 새 레코드에 함께 저장할 부가 필드(예: restarted_from).
    성공 시 201 + {"run_id": ...}.
    """
    # 카탈로그 기반 티커 검증: 통과 시 카탈로그의 정규 티커로 실행한다
    canonical, ticker_error = _validate_ticker(ticker)
    if ticker_error:
        return ticker_error
    ticker = canonical

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
    item = {
        "run_id": run_id,
        "ticker": ticker,
        "analysis_date": analysis_date,
        "depth": depth,
        "status": "queued",
        "created_at": now,
        "updated_at": now,
    }
    if extra:
        item.update(extra)
    table.put_item(Item=item)

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


def create_run(body):
    ticker = str(body.get("ticker", "")).strip().upper()
    analysis_date = str(body.get("analysis_date", "")).strip()
    depth, depth_error = _parse_depth(body.get("depth", 1))
    if depth_error:
        return depth_error

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

    return _start_run(ticker, analysis_date, depth)


def restart_run(run_id, body):
    """기존 실행을 같은 종목·날짜로, 새 깊이(depth)로 다시 실행한다.

    원본 레코드에서 ticker/analysis_date를 복사하고 depth만 요청값으로 바꿔
    create_run과 동일한 검증·동시성·RunTask 경로(_start_run)를 재사용한다.
    새 레코드에는 restarted_from(원본 run_id)을 기록한다.
    """
    original = table.get_item(Key={"run_id": run_id}).get("Item")
    if not original:
        return _err(404, "해당 실행을 찾을 수 없습니다.")

    depth, depth_error = _parse_depth(body.get("depth"))
    if depth_error:
        return depth_error

    ticker = str(original.get("ticker", "")).strip().upper()
    analysis_date = str(original.get("analysis_date", "")).strip()
    return _start_run(ticker, analysis_date, depth, extra={"restarted_from": run_id})


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


def _parse_body(event):
    """POST 본문을 JSON으로 파싱한다. (body, 오류 응답) 튜플을 반환한다."""
    try:
        raw = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            raw = base64.b64decode(raw).decode("utf-8")
        return json.loads(raw), None
    except (ValueError, UnicodeDecodeError):
        return None, _err(400, "요청 본문이 올바른 JSON이 아닙니다.")


def handler(event, _context):
    http = event.get("requestContext", {}).get("http", {})
    method = http.get("method", "GET")
    path = event.get("rawPath", "/")
    query = event.get("queryStringParameters") or {}

    if not check_auth(event):
        return _err(401, "로그인이 필요합니다.")

    try:
        if method == "POST" and path == "/api/runs":
            body, body_error = _parse_body(event)
            if body_error:
                return body_error
            return create_run(body)

        # 기존 실행 재시작 (원본 종목·날짜 유지, 새 깊이). create_run보다 먼저
        # 검사해 아래 상세 조회 라우트(GET)와 경로가 겹치지 않게 한다.
        m = re.match(r"^/api/runs/([a-f0-9]{12})/restart$", path)
        if method == "POST" and m:
            body, body_error = _parse_body(event)
            if body_error:
                return body_error
            return restart_run(m.group(1), body)

        if method == "GET" and path == "/api/runs":
            return list_runs()

        if method == "GET" and path == "/api/catalog":
            return get_catalog(query)

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
