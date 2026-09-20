# ============================================================
# [모듈 개요] G20 매크로 대시보드 API (/api/macro/*, /api/admin/macro/*)
#
# api_handler.py가 인증(관리자 경로는 관리자 게이트까지) 통과한 요청만 이 모듈로 위임한다.
# Lambda zip에는 api_handler.py와 이 파일만 담기고 런타임 의존성은 boto3뿐이라
#  - webui/macro/*(PyYAML 필요)를 임포트하지 않고,
#  - DynamoDB 키 규칙(CONTRACT 4장)과 지표 id 목록(2장)을 이 파일에 다시 선언한다.
# 계약을 바꿀 때는 webui/macro/CONTRACT.md → 이 파일 → webui/frontend/macro.js 순서로 고친다.
#
# 엔드포인트 (CONTRACT 9장)
#   GET  /api/macro/meta             국가·지표 사전 + 소스별 마지막 수집 상태
#   GET  /api/macro/overview         국가×지표 히트맵 (LATEST 지표당 Query 1회 + 중앙값)
#   GET  /api/macro/countries/{iso}  국가 프로필 (SNAPSHOT Get 1회, 없으면 LATEST 폴백)
#   GET  /api/macro/series           다국가 시계열 (transform=level|yoy|index100)
#   GET  /api/macro/observations     관측치 상세(개정 이력) + 관련 정성 문서
#   GET  /api/macro/docs             정성 문서 목록 (최신순)
#   GET  /api/macro/politics/{iso}   선거·poll_of_polls·여론조사 원본·정당별 월 시계열
#   GET  /api/macro/fx               통화가치 지수(기준=100) + 기간 변화율 + DXY
#   POST /api/admin/macro/refresh    수동 재수집 (워커 RunTask, command만 교체 · 수집 락 있으면 409)
#   POST /api/admin/macro/review     AI 문서 승인/반려 (review_status 갱신)
#
# 규칙
#  - 응답 형식은 api_handler와 동일: 성공 `{"ok": true, ...}` / 실패 `{"error": "한국어 메시지"}`
#  - DynamoDB Decimal은 모두 float/int로 바꿔 내려준다(_plain) — 응답에 Decimal을 남기지 않는다.
#  - 모든 Query는 LastEvaluatedKey를 따라가며 필요한 만큼 모은다(_query_all).
#  - 유로 회원국(DE/FR/IT)의 정책금리·통화량·환율은 EU 값을 복제하고 flags에
#    `euro_area_shared`를 붙인다 (CONTRACT 1장). 수집기는 이 지표를 회원국별로 모으지 않는다.
#  - 국가·지표 사전과 수집 로그는 Lambda 컨테이너에 5분 캐시한다 (요청마다 38+14 Get 방지).
#  - 기간 상한은 항상 강제한다: from 생략 시 (to 또는 현재) − 30년을 기본 시작으로 넣고, 일별(D)은
#    10년, 국가당 포인트 수는 4,000점까지. /fx의 base도 같은 규칙(30년·D 10년)을 따른다.
#
# 사용 예 (api_handler.py):
#   api = MacroApi(table=dynamodb.Table(MACRO_TABLE_NAME), s3=s3, bucket=DATA_BUCKET,
#                  run_task=_run_worker_command)
#   return api.handle(method, path, query)
# ============================================================
import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import unquote

from boto3.dynamodb.conditions import Attr, Key
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError

# ------------------------------------------------------------ 계약 상수 (CONTRACT 2·5·12장)

# CONTRACT 2장 "지표 id 고정 목록" (webui/macro/registry.py INDICATOR_IDS와 같은 순서).
# SERIES# 메타는 pk가 지표마다 달라 Query로 한 번에 못 읽으므로(GSI 없음, scan 금지)
# 이 목록으로 BatchGetItem을 던진다.
INDICATOR_IDS = (
    "policy_rate", "fx_usd", "fx_value_index", "m2_level", "m2_yoy",
    "cpi_index", "cpi_yoy", "core_cpi_yoy", "ppi_index", "ppi_yoy",
    "house_price_index", "house_price_yoy", "gdp_usd", "gdp_growth",
    "gni_usd", "gni_pc", "gov_expense_gdp", "gov_revenue_gdp", "gov_debt_gdp",
    "mil_gdp", "mil_expenditure_share", "mil_usd", "va_agri", "va_industry",
    "va_manuf", "va_services", "exports_gdp", "exports_top_hs2", "elec_mix",
    "energy_import_dep", "fuel_dep_oil", "fuel_dep_gas", "fuel_dep_coal",
    "party_support", "gov_approval", "cb_stance", "top_companies", "dxy",
)

# CONTRACT 9장 개요 기본 지표 9개
DEFAULT_OVERVIEW_INDICATORS = (
    "policy_rate", "cpi_yoy", "ppi_yoy", "house_price_yoy", "fx_value_index",
    "m2_yoy", "gdp_growth", "mil_expenditure_share", "party_support",
)

# CONTRACT 12장 수집 소스 (INGEST#<source> 키 + 관리자 refresh 화이트리스트)
SOURCE_NAMES = (
    "yahoo", "bis", "cb_statements", "oecd", "imf", "fred", "ember", "owid",
    "worldbank", "wits", "companies", "wiki_polls", "energy_policy", "weekly_brief",
)

# CONTRACT 5장 문서 type
DOC_TYPES = (
    "cb_stance", "poll", "poll_of_polls", "election", "energy_policy",
    "weekly_brief", "not_applicable",
)
# 국가 프로필(SNAPSHOT.docs)에 담기는 문서 type (store.rebuild_snapshot과 동일)
SNAPSHOT_DOC_TYPES = ("cb_stance", "energy_policy", "election", "poll_of_polls", "not_applicable")

FREQS = ("D", "W", "M", "Q", "Y", "E")
_PERIOD_RE = {
    "D": re.compile(r"^\d{4}-\d{2}-\d{2}$"),
    "W": re.compile(r"^\d{4}-\d{2}-\d{2}$"),
    "E": re.compile(r"^\d{4}-\d{2}-\d{2}$"),
    "M": re.compile(r"^\d{4}-\d{2}$"),
    "Q": re.compile(r"^\d{4}-Q[1-4]$"),
    "Y": re.compile(r"^\d{4}$"),
}
_PERIOD_HINT = {
    "D": "YYYY-MM-DD", "W": "YYYY-MM-DD", "E": "YYYY-MM-DD",
    "M": "YYYY-MM", "Q": "YYYY-Qn", "Y": "YYYY",
}
# 전년비 lag (CONTRACT 6장: 같은 빈도의 12개월/4분기/1년 전). 일별(D)은 전년비를 만들지 않는다.
YOY_LAG = {"M": 12, "Q": 4, "Y": 1, "W": 52}
TRANSFORMS = ("level", "yoy", "index100")

_ISO_RE = re.compile(r"^[A-Z]{2}$")
_DOC_ISO_RE = re.compile(r"^[A-Z0-9]{2,4}$")  # 국가 iso + 주간 브리프(G20)
_DATE_RE = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")

# 쿼리 제한 (CONTRACT 9장)
MAX_COUNTRIES = 20
MAX_INDICATORS = 20
MAX_YEARS = 30
# 일별(D) 시계열은 한 국가 30년이면 7,000점을 넘어 Lambda 응답·프론트 렌더가 무거워진다
MAX_DAILY_YEARS = 10
MAX_POINTS_PER_COUNTRY = 4000
MAX_DOC_LIMIT = 100
DEFAULT_DOC_LIMIT = 12
# 관측치 드로어의 관련 문서 건수, 정치 화면의 여론조사 원본 건수
RELATED_CB_DOCS = 3
RELATED_POLLS = 5
POLITICS_POLLS = 30
POLITICS_DEFAULT_MONTHS = 36
FX_DEFAULT_DAYS = 365
# 국가·지표 사전과 수집 로그의 컨테이너 캐시 수명(초)
META_CACHE_TTL = 300.0
# BatchGetItem 1회 최대 키 수 / UnprocessedKeys 재시도 상한
_BATCH_SIZE = 25
_BATCH_RETRY = 5
# sk 범위 질의의 상한 센티넬 (store.py와 동일 규칙)
SK_MAX = "￿"
EURO_REF = "EU"
# 수집기 동시 실행 락 (CONTRACT 4장 `CONFIG / LOCK#collect`) — 만료 전이면 refresh를 거절한다
LOCK_PK, LOCK_SK = "CONFIG", "LOCK#collect"
# 국가 사전(SERIES#__countries__)을 아직 못 읽은 경우에만 쓰는 CONTRACT 1장 유로 회원국
FALLBACK_EURO_MEMBERS = ("DE", "FR", "IT")
EURO_FLAG = "euro_area_shared"

_INDICATOR_META_FIELDS = ("id", "name_ko", "unit", "category", "native_freq", "agg", "decimals")
_COUNTRY_META_FIELDS = ("iso", "iso3", "name_ko", "name_en", "ccy")

_DESERIALIZER = TypeDeserializer()


def is_euro_shared(indicator: str) -> bool:
    """유로존 단일값 지표인가 (CONTRACT 1장: policy_rate, m2_*, fx_*)."""
    return indicator == "policy_rate" or indicator.startswith(("m2_", "fx_"))


EURO_SHARED_IDS = tuple(i for i in INDICATOR_IDS if is_euro_shared(i))


# ------------------------------------------------------------------- 응답 헬퍼


def _plain(value):
    """DynamoDB의 Decimal 등을 JSON 직렬화 가능한 값으로 변환한다 (api_handler와 동일)."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
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


def _ok(body):
    return _resp(200, {"ok": True, **body})


def _err(status, message):
    return _resp(status, {"error": message})


# --------------------------------------------------------------- 값·기간 유틸


def _num(value):
    """숫자(bool 제외)면 그대로, 아니면 None. Decimal은 미리 _plain을 통과시킨다."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _round(value, digits=6):
    return round(float(value), digits)


def _median(values):
    vals = sorted(v for v in values if _num(v) is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    return float(vals[mid]) if len(vals) % 2 else _round((vals[mid - 1] + vals[mid]) / 2.0)


def _csv(raw):
    """`a,b , c` → ["a", "b", "c"]"""
    return [tok.strip() for tok in str(raw or "").split(",") if tok.strip()]


def _dedup(items):
    out = []
    for i in items:
        if i not in out:
            out.append(i)
    return out


def _valid_period(freq, period):
    pattern = _PERIOD_RE.get(freq)
    return bool(pattern and pattern.match(str(period or "")))


def _period_key(freq, period):
    """같은 빈도 안에서 시간순 비교에 쓰는 키 (aggregate.period_sort_key와 동일 규칙)."""
    s = str(period or "")
    try:
        if freq in ("D", "W", "E"):
            y, m, d = s.split("-")
            return (int(y), int(m), int(d))
        if freq == "M":
            y, m = s.split("-")
            return (int(y), int(m), 0)
        if freq == "Q":
            y, q = s.split("-Q")
            return (int(y), int(q), 0)
        return (int(s), 0, 0)
    except (ValueError, TypeError):
        return (0, 0, 0)


def _shift_period(freq, period, n):
    """n기간 전 period (aggregate.previous_period와 동일 규칙)."""
    if freq in ("D", "W", "E"):
        step = 7 if freq == "W" else 1
        return (date.fromisoformat(period) - timedelta(days=step * int(n))).isoformat()
    if freq == "M":
        y, m = (int(x) for x in str(period).split("-"))
        total = y * 12 + (m - 1) - int(n)
        return f"{total // 12:04d}-{total % 12 + 1:02d}"
    if freq == "Q":
        y, q = (int(x) for x in str(period).split("-Q"))
        total = y * 4 + (q - 1) - int(n)
        return f"{total // 4:04d}-Q{total % 4 + 1}"
    return f"{int(period) - int(n):04d}"


def _current_period(freq, now):
    """현재 시각이 속한 기간 문자열 (기간 상한의 기본 종점)."""
    d = now.date() if isinstance(now, datetime) else now
    if freq == "M":
        return f"{d.year:04d}-{d.month:02d}"
    if freq == "Q":
        return f"{d.year:04d}-Q{(d.month - 1) // 3 + 1}"
    if freq == "Y":
        return f"{d.year:04d}"
    return d.isoformat()


def _years_before(freq, period, years):
    """period에서 years년 전 같은 형식의 기간 (from 생략 시 기본 시작 계산)."""
    if freq in ("D", "W", "E"):
        d = date.fromisoformat(period)
        try:
            return d.replace(year=d.year - int(years)).isoformat()
        except ValueError:  # 2월 29일
            return d.replace(year=d.year - int(years), day=28).isoformat()
    if freq == "M":
        return _shift_period("M", period, 12 * int(years))
    if freq == "Q":
        return _shift_period("Q", period, 4 * int(years))
    return _shift_period("Y", period, int(years))


def _max_years(freq):
    return MAX_DAILY_YEARS if freq == "D" else MAX_YEARS


def _nearest_earlier(freq, points, target):
    """target 기간 이하에서 가장 늦은 관측 (일·주 빈도의 전년비·지수 기준값 보정용)."""
    want = _period_key(freq, target)
    best = None
    for p in points:
        if p.get("value") is None:
            continue
        key = _period_key(freq, p["period"])
        if key <= want and (best is None or key > _period_key(freq, best["period"])):
            best = p
    return best


def _point(period, value, flags=None):
    out = {"period": period, "value": value}
    if flags:
        out["flags"] = list(flags)
    return out


def _with_euro_flag(row, iso):
    """EU 항목을 유로 회원국 값으로 복제한다 (iso 교체 + euro_area_shared 플래그)."""
    out = _plain(row)
    out.pop("pk", None)
    out.pop("sk", None)
    out["iso"] = iso
    out["euro_ref"] = EURO_REF
    flags = list(out.get("flags") or [])
    if EURO_FLAG not in flags:
        flags.append(EURO_FLAG)
    out["flags"] = flags
    return out


def _cell(row):
    """개요 히트맵 셀 (CONTRACT 9장 + 프론트: freq·period는 드로어 열기에 필수)."""
    r = _plain(row)
    cell = {
        "value": _num(r.get("value")),
        "period": r.get("period"),
        "freq": r.get("freq"),
        "change": _num(r.get("change")),
        "change_pct": _num(r.get("change_pct")),
        "rank": _num(r.get("rank")),
        "n": _num(r.get("n")),
        "flags": list(r.get("flags") or []),
        "source": r.get("source"),
        "unit": r.get("unit"),
    }
    if r.get("payload") is not None:
        cell["payload"] = r["payload"]
    return cell


def _indicator_meta_view(item):
    """SERIES#<id>/META → /meta·/series의 indicators 항목 (registry.to_meta의 부분집합)."""
    row = _plain(item)
    out = {k: row.get(k) for k in _INDICATOR_META_FIELDS}
    out["store_freqs"] = list(row.get("store_freqs") or [])
    out["composite"] = bool(row.get("composite"))
    return out


def _country_meta_view(row):
    out = {k: row.get(k) for k in _COUNTRY_META_FIELDS}
    out["iso"] = str(row.get("iso") or "").upper()
    out["groups"] = [str(g) for g in (row.get("groups") or [])]
    out["euro"] = bool(row.get("euro"))
    return out


def _parse_countries(raw):
    """countries 쿼리 파싱. (isos, 오류 응답) 튜플."""
    isos = []
    for tok in _csv(raw):
        iso = tok.upper()
        if not _ISO_RE.match(iso):
            return None, _err(400, f"국가 코드가 올바르지 않습니다: {tok}")
        if iso not in isos:
            isos.append(iso)
    if not isos:
        return None, _err(400, "countries 파라미터가 필요합니다 (예: KR,US).")
    if len(isos) > MAX_COUNTRIES:
        return None, _err(400, f"국가는 최대 {MAX_COUNTRIES}개까지 조회할 수 있습니다.")
    return isos, None


def _parse_limit(raw, default, maximum):
    if raw is None or str(raw).strip() == "":
        return default, None
    try:
        limit = int(str(raw).strip())
    except (TypeError, ValueError):
        return None, _err(400, f"limit은 1~{maximum} 사이의 정수여야 합니다.")
    if limit < 1 or limit > maximum:
        return None, _err(400, f"limit은 1~{maximum} 사이의 정수여야 합니다.")
    return limit, None


def _check_span(freq, from_period, to_period, now):
    """기간 제한(최대 30년, 일별은 10년) 검사. 위반 시 오류 응답, 아니면 None.

    호출자는 from을 생략한 요청에도 기본 시작(_years_before)을 채운 뒤 이 검사를 항상 거친다.
    """
    if not from_period:
        return None
    start = _period_key(freq, from_period)[0]
    end = _period_key(freq, to_period)[0] if to_period else now.year
    if end < start:
        return _err(400, "from이 to보다 늦습니다.")
    limit = _max_years(freq)
    if end - start > limit:
        if freq == "D":
            return _err(400, f"일별(D) 조회 기간은 최대 {MAX_DAILY_YEARS}년입니다.")
        return _err(400, f"조회 기간은 최대 {MAX_YEARS}년입니다.")
    return None


def _to_yoy(freq, points, lag):
    """같은 빈도의 lag기간 전 값 대비 %(CONTRACT 6장). 기준값이 없으면 그 기간은 생략."""
    by_period = {p["period"]: p for p in points}
    out = []
    for p in points:
        if p.get("value") is None:
            continue
        base_period = _shift_period(freq, p["period"], lag)
        base = by_period.get(base_period)
        if base is None and freq in ("D", "W", "E"):
            # 일·주 빈도는 정확히 1년 전 관측이 없는 경우가 많아 최근접 이전 관측을 쓴다
            base = _nearest_earlier(freq, points, base_period)
        if base is None or not base.get("value"):
            continue
        out.append(_point(p["period"], _round(p["value"] / base["value"] * 100.0 - 100.0),
                          p.get("flags")))
    return out


def _to_index100(freq, points, base_period):
    """기준 기간=100 지수. 기준 기간 관측이 없으면 최근접 이전 관측을 기준으로 쓴다."""
    valued = [p for p in points if p.get("value") is not None]
    if not valued:
        return [], None
    ref = _nearest_earlier(freq, valued, base_period) if base_period else None
    if ref is None:
        ref = valued[0]  # 기준 기간 이전 관측이 없으면 가장 이른 관측이 기준
    if not ref.get("value"):
        return [], None
    out = [
        _point(p["period"], _round(p["value"] / ref["value"] * 100.0), p.get("flags"))
        for p in valued
    ]
    return out, ref["period"]


class MacroApi:
    """`/api/macro/*`와 `/api/admin/macro/*`의 구현 (CONTRACT 9장).

    table은 boto3 DynamoDB Table(또는 macro.fakeddb.FakeTable), run_task는
    워커 command를 받아 태스크 ARN을 돌려주는 콜러블(api_handler._run_worker_command).
    now는 테스트 결정성을 위한 시계 주입(datetime 반환).
    """

    def __init__(self, table, s3=None, bucket=None, run_task=None, now=None):
        self.table = table
        self.s3 = s3
        self.bucket = bucket
        self.run_task = run_task
        self._now_fn = now or (lambda: datetime.now(timezone.utc))
        # 컨테이너 캐시: (적재 시각, 값)
        self._countries_cache = None
        self._ingest_cache = None
        self._indicator_cache = {}

    # ------------------------------------------------------------- 라우팅

    def handle(self, method, path, query):
        """GET /api/macro/* 라우팅. Lambda 응답(dict)을 돌려준다."""
        query = query or {}
        try:
            if method != "GET":
                return _err(405, "매크로 조회 API는 GET만 지원합니다.")
            sub = str(path or "")[len("/api/macro"):] or "/"
            if sub == "/meta":
                return self._meta()
            if sub == "/overview":
                return self._overview(query)
            if sub == "/series":
                return self._series(query)
            if sub == "/observations":
                return self._observations(query)
            if sub == "/docs":
                return self._docs(query)
            if sub == "/fx":
                return self._fx(query)
            m = re.match(r"^/countries/([^/]+)$", sub)
            if m:
                return self._country_profile(unquote(m.group(1)))
            m = re.match(r"^/politics/([^/]+)$", sub)
            if m:
                return self._politics(unquote(m.group(1)), query)
            return _err(404, "존재하지 않는 API 경로입니다.")
        except Exception as e:  # noqa: BLE001 - api_handler와 동일하게 500으로 봉인
            print(f"macro api error: {method} {path}: {e}")
            return _err(500, "매크로 데이터를 불러오는 중 오류가 발생했습니다.")

    def handle_admin(self, method, path, body, actor):
        """POST /api/admin/macro/* 라우팅 (관리자 게이트는 api_handler가 통과시킨다)."""
        body = body if isinstance(body, dict) else {}
        try:
            if method != "POST":
                return _err(405, "매크로 관리자 API는 POST만 지원합니다.")
            if path == "/api/admin/macro/refresh":
                return self._refresh(body)
            if path == "/api/admin/macro/review":
                return self._review(body, actor)
            return _err(404, "존재하지 않는 API 경로입니다.")
        except Exception as e:  # noqa: BLE001
            print(f"macro admin api error: {method} {path}: {e}")
            return _err(500, "매크로 관리자 요청 처리 중 오류가 발생했습니다.")

    # ------------------------------------------------------- DynamoDB 접근

    def _now(self):
        v = self._now_fn()
        if isinstance(v, datetime):
            return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
        return datetime.fromisoformat(str(v))

    def _get(self, pk, sk):
        return self.table.get_item(Key={"pk": pk, "sk": sk}).get("Item")

    def _query_all(self, cond, forward=True, limit=None):
        """LastEvaluatedKey를 따라가며 모든 페이지를 모은다 (limit 건까지)."""
        out = []
        start = None
        while True:
            kw = {"KeyConditionExpression": cond, "ScanIndexForward": forward}
            if limit is not None:
                remaining = limit - len(out)
                if remaining <= 0:
                    break
                kw["Limit"] = remaining
            if start:
                kw["ExclusiveStartKey"] = start
            resp = self.table.query(**kw)
            out.extend(resp.get("Items", []))
            start = resp.get("LastEvaluatedKey")
            if not start:
                break
        return out[:limit] if limit is not None else out

    def _batch_get(self, keys):
        """pk가 서로 다른 항목들을 BatchGetItem(25건 단위)으로 모아 읽는다.

        주입받는 것이 Table뿐이라 저수준 client(table.meta.client)를 쓰고, 키는 문자열만
        이므로 타입 태그를 직접 붙이고 응답만 TypeDeserializer로 되돌린다. client가 없거나
        (페이크 테이블) 호출이 실패하면 get_item 반복으로 폴백한다.
        """
        if not keys:
            return []
        client = getattr(getattr(self.table, "meta", None), "client", None)
        name = getattr(self.table, "table_name", "") or getattr(self.table, "name", "")
        if client is not None and name:
            try:
                return self._batch_get_via_client(client, name, keys)
            except Exception as e:  # noqa: BLE001 - 조회 실패는 get_item으로 대체 가능
                print(f"macro api: BatchGetItem 실패, get_item으로 폴백합니다: {e}")
        out = []
        for key in keys:
            item = self.table.get_item(Key=key).get("Item")
            if item:
                out.append(item)
        return out

    @staticmethod
    def _batch_get_via_client(client, name, keys):
        out = []
        for i in range(0, len(keys), _BATCH_SIZE):
            request = {
                name: {"Keys": [{k: {"S": str(v)} for k, v in key.items()}
                                for key in keys[i:i + _BATCH_SIZE]]}
            }
            for _ in range(_BATCH_RETRY):
                resp = client.batch_get_item(RequestItems=request)
                for raw in resp.get("Responses", {}).get(name, []):
                    out.append({k: _DESERIALIZER.deserialize(v) for k, v in raw.items()})
                request = resp.get("UnprocessedKeys") or None
                if not request:
                    break
        return out

    # --------------------------------------------------- 사전·수집 로그 캐시

    @staticmethod
    def _fresh(cached):
        return cached is not None and (time.time() - cached[0]) < META_CACHE_TTL

    def _countries(self):
        """`SERIES#__countries__`/META의 국가 목록 (5분 캐시). 없으면 빈 목록."""
        if self._fresh(self._countries_cache):
            return self._countries_cache[1]
        item = self._get("SERIES#__countries__", "META") or {}
        rows = []
        for raw in _plain(item.get("countries") or []):
            if isinstance(raw, dict) and raw.get("iso"):
                rows.append(_country_meta_view(raw))
        self._countries_cache = (time.time(), rows)
        return rows

    def _country_view(self, iso):
        for c in self._countries():
            if c["iso"] == iso:
                return c
        return {"iso": iso}

    def _euro_members(self):
        """유로 회원국 집합 (DE/FR/IT). 국가 사전을 못 읽으면 CONTRACT 1장 상수로 폴백."""
        rows = self._countries()
        if not rows:
            return set(FALLBACK_EURO_MEMBERS)
        return {c["iso"] for c in rows if c.get("euro")}

    def _indicator_metas(self, ids):
        """지표 META를 BatchGet으로 읽어 5분 캐시한다. 반환: {id: meta}"""
        now = time.time()
        want = [i for i in ids if not self._fresh(self._indicator_cache.get(i))]
        if want:
            fetched = {}
            for item in self._batch_get([{"pk": f"SERIES#{i}", "sk": "META"} for i in want]):
                meta = _indicator_meta_view(item)
                if meta.get("id"):
                    fetched[meta["id"]] = meta
            for i in want:
                self._indicator_cache[i] = (now, fetched.get(i))
        out = {}
        for i in ids:
            cached = self._indicator_cache.get(i)
            if cached and cached[1]:
                out[i] = cached[1]
        return out

    def _ingest_status(self):
        """소스별 마지막 수집 요약 (`INGEST#<source>`/LATEST, 5분 캐시).

        기록이 아직 없는 소스는 응답에서 빼서 프론트의 "소스 n/m 정상" 계산이
        실행되지 않은 소스를 실패로 세지 않게 한다.
        """
        if self._fresh(self._ingest_cache):
            return self._ingest_cache[1]
        out = {}
        keys = [{"pk": f"INGEST#{s}", "sk": "LATEST"} for s in SOURCE_NAMES]
        for item in self._batch_get(keys):
            row = _plain(item)
            source = str(row.get("source") or str(row.get("pk") or "")[len("INGEST#"):])
            if not source:
                continue
            errors = row.get("errors") or []
            out[source] = {
                "status": row.get("status"),
                "started_at": row.get("started_at"),
                "finished_at": row.get("finished_at"),
                "n_obs": _num(row.get("n_obs")),
                "n_docs": _num(row.get("n_docs")),
                "next_due": row.get("next_due"),
                "errors_count": len(errors) if isinstance(errors, list) else 0,
            }
        self._ingest_cache = (time.time(), out)
        return out

    def _asof(self):
        """개요 기준 기간: 현재 UTC 기준 전월·전분기·전년 (완결된 마지막 기간)."""
        now = self._now()
        prev_month = now.month - 1 or 12
        month_year = now.year if now.month > 1 else now.year - 1
        quarter = (now.month - 1) // 3 + 1
        prev_quarter = quarter - 1 or 4
        quarter_year = now.year if quarter > 1 else now.year - 1
        return {
            "M": f"{month_year:04d}-{prev_month:02d}",
            "Q": f"{quarter_year:04d}-Q{prev_quarter}",
            "Y": f"{now.year - 1:04d}",
        }

    def _group_isos(self, group, exclude=()):
        """국가군 필터. (isos, 오류 응답) — 국가 사전이 없으면 isos는 None(전체)."""
        countries = self._countries()
        if not countries:
            return None, None
        groups = {g for c in countries for g in c["groups"]}
        if group not in groups:
            return None, _err(400, f"알 수 없는 국가군입니다: {group} "
                                   f"(가능: {', '.join(sorted(groups))})")
        isos = [c["iso"] for c in countries if group in c["groups"] and c["iso"] not in exclude]
        return isos, None

    def _list_docs(self, doc_type, iso, limit):
        rows = self._query_all(Key("pk").eq(f"DOC#{doc_type}#{iso}"), forward=False, limit=limit)
        return [_plain(r) for r in rows]

    def _obs_points(self, indicator, iso, freq, from_period, to_period):
        """`OBS#<ind>#<iso>`의 한 빈도 시계열을 시간 오름차순 point 목록으로."""
        pk = f"OBS#{indicator}#{iso}"
        if from_period is None and to_period is None:
            cond = Key("pk").eq(pk) & Key("sk").begins_with(f"{freq}#")
        else:
            lo = f"{freq}#{from_period}" if from_period else f"{freq}#"
            hi = f"{freq}#{to_period}{SK_MAX}" if to_period else f"{freq}#{SK_MAX}"
            cond = Key("pk").eq(pk) & Key("sk").between(lo, hi)
        out = []
        for item in self._query_all(cond):
            row = _plain(item)
            period = str(row.get("period") or "")
            if not period:
                continue
            point = _point(period, _num(row.get("value")), row.get("flags"))
            if row.get("payload") is not None:
                point["payload"] = row["payload"]
            out.append(point)
        return out

    # ------------------------------------------------------ GET /macro/meta

    def _meta(self):
        metas = self._indicator_metas(INDICATOR_IDS)
        return _ok({
            "countries": self._countries(),
            "indicators": [metas[i] for i in INDICATOR_IDS if i in metas],
            "ingest": self._ingest_status(),
        })

    # -------------------------------------------------- GET /macro/overview

    def _overview(self, query):
        group = str(query.get("group") or "G20").strip().upper()
        ids = _dedup(_csv(query.get("indicators"))) or list(DEFAULT_OVERVIEW_INDICATORS)
        unknown = [i for i in ids if i not in INDICATOR_IDS]
        if unknown:
            return _err(400, f"알 수 없는 지표입니다: {', '.join(unknown[:3])}")
        if len(ids) > MAX_INDICATORS:
            return _err(400, f"지표는 최대 {MAX_INDICATORS}개까지 조회할 수 있습니다.")
        isos, error = self._group_isos(group)
        if error:
            return error

        euro_members = self._euro_members()
        by_indicator = {}
        for indicator in ids:
            rows = {}
            for item in self._query_all(Key("pk").eq(f"LATEST#{indicator}")):
                rows[str(item.get("sk") or "")] = item
            # 유로 공유 지표는 회원국 항목이 없으므로 EU 값을 복제해 채운다 (CONTRACT 1장)
            if is_euro_shared(indicator) and rows.get(EURO_REF):
                for member in euro_members:
                    if member not in rows:
                        rows[member] = _with_euro_flag(rows[EURO_REF], member)
            by_indicator[indicator] = rows

        if isos is None:
            # 국가 사전이 아직 없으면 LATEST에 있는 국가만이라도 보여준다
            isos = sorted({iso for rows in by_indicator.values() for iso in rows if iso})

        values = {i: [] for i in ids}
        rows_out = []
        for iso in isos:
            cells = {}
            for indicator in ids:
                row = by_indicator[indicator].get(iso)
                if not row:
                    continue
                cell = _cell(row)
                cells[indicator] = cell
                if cell["value"] is not None:
                    values[indicator].append(cell["value"])
            rows_out.append({"iso": iso, "cells": cells})

        metas = self._indicator_metas(ids)
        return _ok({
            "group": group,
            "asof": self._asof(),
            # 프론트는 indicators를 메타 객체 배열로 읽는다 (name_ko·unit·decimals·store_freqs)
            "indicators": [metas.get(i) or {"id": i} for i in ids],
            "rows": rows_out,
            # 중앙값은 화면에 실제로 표시되는 국가(국가군 필터 + 유로 복제) 기준
            "median": {i: _median(values[i]) for i in ids},
        })

    # ------------------------------------------- GET /macro/countries/{iso}

    def _country_profile(self, iso_raw):
        iso = str(iso_raw or "").strip().upper()
        if not _ISO_RE.match(iso):
            return _err(400, "국가 코드는 2자리 ISO 코드여야 합니다 (예: KR).")
        latest, docs, source = self._profile_parts(iso)
        body = {
            "country": self._country_view(iso),
            "latest": latest,
            "docs": docs,
            "profile_source": source,
        }
        if iso in self._euro_members():
            # 금리·통화량·환율과 중앙은행 기조는 유로존(EU) 것을 참조한다
            eu_latest, eu_docs, _ = self._profile_parts(
                EURO_REF, indicators=EURO_SHARED_IDS, doc_types=("cb_stance",)
            )
            for indicator in EURO_SHARED_IDS:
                row = eu_latest.get(indicator)
                if row and indicator not in latest:
                    latest[indicator] = _with_euro_flag(row, iso)
            if "cb_stance" not in docs and eu_docs.get("cb_stance"):
                docs["cb_stance"] = eu_docs["cb_stance"]
            body["euro_ref"] = EURO_REF
        return _ok(body)

    def _profile_parts(self, iso, indicators=INDICATOR_IDS, doc_types=SNAPSHOT_DOC_TYPES):
        """국가 프로필의 (latest, docs, 출처). SNAPSHOT Get 1회, 없으면 LATEST/DOC 폴백."""
        snapshot = self._get(f"SNAPSHOT#{iso}", "PROFILE")
        if snapshot:
            row = _plain(snapshot)
            return dict(row.get("latest") or {}), dict(row.get("docs") or {}), "snapshot"
        # 첫 배포 직후처럼 SNAPSHOT이 아직 없으면 LATEST(지표별 1건)와 문서로 직접 구성
        latest = {}
        for item in self._batch_get([{"pk": f"LATEST#{i}", "sk": iso} for i in indicators]):
            row = _plain(item)
            pk = str(row.get("pk") or "")
            indicator = str(row.get("indicator") or pk[len("LATEST#"):])
            row.pop("pk", None)
            row.pop("sk", None)
            if indicator:
                latest[indicator] = row
        docs = {}
        for doc_type in doc_types:
            found = self._list_docs(doc_type, iso, 1)
            if found:
                docs[doc_type] = found[0]
        return latest, docs, "fallback"

    # ---------------------------------------------------- GET /macro/series

    def _series(self, query):
        indicator = str(query.get("indicator") or "").strip()
        if indicator not in INDICATOR_IDS:
            return _err(400, "indicator 파라미터가 필요합니다 (CONTRACT 2장 지표 id).")
        isos, error = _parse_countries(query.get("countries"))
        if error:
            return error
        freq = str(query.get("freq") or "M").strip().upper()
        if freq not in FREQS:
            return _err(400, f"freq는 {', '.join(FREQS)} 중 하나여야 합니다.")
        from_period = str(query.get("from") or "").strip() or None
        to_period = str(query.get("to") or "").strip() or None
        for label, period in (("from", from_period), ("to", to_period)):
            if period and not _valid_period(freq, period):
                return _err(400, f"{label}은 {freq} 빈도 형식({_PERIOD_HINT[freq]})이어야 합니다.")
        now = self._now()
        if not from_period:
            # from 생략 = "(to 또는 현재) − 상한"부터. 전 이력 무제한 조회는 허용하지 않는다.
            from_period = _years_before(freq, to_period or _current_period(freq, now), _max_years(freq))
        error = _check_span(freq, from_period, to_period, now)
        if error:
            return error
        transform = str(query.get("transform") or "level").strip().lower()
        if transform not in TRANSFORMS:
            return _err(400, f"transform은 {', '.join(TRANSFORMS)} 중 하나여야 합니다.")
        base = str(query.get("base") or "").strip() or None
        if base and not _valid_period(freq, base):
            return _err(400, f"base는 {freq} 빈도 형식({_PERIOD_HINT[freq]})이어야 합니다.")
        lag = None
        if transform == "yoy":
            lag = YOY_LAG.get(freq)
            if lag is None:
                return _err(400, f"{freq} 빈도는 전년비(yoy)를 계산하지 않습니다.")
        if transform == "index100" and base is None:
            base = from_period

        # 변환에 필요한 창 확장: yoy는 lag기간 전 값, index100은 기준 기간 값이 필요하다
        query_from = from_period
        if from_period and transform == "yoy":
            query_from = _shift_period(freq, from_period, lag)
        if (transform == "index100" and from_period and base
                and _period_key(freq, base) < _period_key(freq, from_period)):
            query_from = base

        euro_members = self._euro_members()
        shared = is_euro_shared(indicator)
        raw_cache = {}
        series = {}
        euro_shared = []
        base_used = {}
        for iso in isos:
            src = EURO_REF if (shared and iso in euro_members) else iso
            if src not in raw_cache:
                raw_cache[src] = self._obs_points(indicator, src, freq, query_from, to_period)
            points = [dict(p) for p in raw_cache[src]]
            if src != iso:
                euro_shared.append(iso)
                for p in points:
                    flags = list(p.get("flags") or [])
                    if EURO_FLAG not in flags:
                        flags.append(EURO_FLAG)
                    p["flags"] = flags
            if transform == "yoy":
                points = _to_yoy(freq, points, lag)
            elif transform == "index100":
                points, used = _to_index100(freq, points, base)
                if used:
                    base_used[iso] = used
            start = _period_key(freq, from_period)
            points = [p for p in points if _period_key(freq, p["period"]) >= start]
            if len(points) > MAX_POINTS_PER_COUNTRY:
                return _err(400, f"국가당 관측치는 최대 {MAX_POINTS_PER_COUNTRY:,}점까지 조회할 수 있습니다. "
                                 f"기간(from/to)을 좁히거나 더 낮은 빈도를 사용하세요.")
            series[iso] = points

        metas = self._indicator_metas([indicator])
        body = {
            "indicator": metas.get(indicator) or {"id": indicator},
            "freq": freq,
            "from": from_period,
            "to": to_period,
            "transform": transform,
            "series": series,
            "euro_shared": euro_shared,
        }
        if transform == "index100":
            body["base"] = base
            body["base_used"] = base_used
        return _ok(body)

    # ---------------------------------------------- GET /macro/observations

    def _observations(self, query):
        indicator = str(query.get("indicator") or "").strip()
        if indicator not in INDICATOR_IDS:
            return _err(400, "indicator 파라미터가 필요합니다 (CONTRACT 2장 지표 id).")
        iso = str(query.get("iso") or "").strip().upper()
        if not _ISO_RE.match(iso):
            return _err(400, "iso 파라미터는 2자리 ISO 코드여야 합니다 (예: KR).")
        freq = str(query.get("freq") or "").strip().upper()
        if freq not in FREQS:
            return _err(400, f"freq는 {', '.join(FREQS)} 중 하나여야 합니다.")
        period = str(query.get("period") or "").strip()
        if not _valid_period(freq, period):
            return _err(400, f"period는 {freq} 빈도 형식({_PERIOD_HINT[freq]})이어야 합니다.")

        resolved = EURO_REF if (is_euro_shared(indicator) and iso in self._euro_members()) else iso
        item = self._get(f"OBS#{indicator}#{resolved}", f"{freq}#{period}")
        if not item:
            return _err(404, "해당 관측치를 찾을 수 없습니다.")
        obs = _plain(item)
        obs.pop("pk", None)
        obs.pop("sk", None)
        obs.setdefault("revisions", [])
        if resolved != iso:
            flags = list(obs.get("flags") or [])
            if EURO_FLAG not in flags:
                flags.append(EURO_FLAG)
            obs["flags"] = flags
        return _ok({
            "observation": obs,
            "resolved_iso": resolved,
            "related_docs": self._related_docs(indicator, iso, resolved),
        })

    def _related_docs(self, indicator, iso, resolved):
        """관측치와 같은 화면에 붙일 정성 문서 (드로어 하단)."""
        if indicator == "policy_rate" or indicator == "cb_stance" or indicator.startswith("m2_"):
            # 유로 회원국의 금리·통화량은 ECB 결정문(iso=EU)이 근거 문서다
            return self._list_docs("cb_stance", resolved, RELATED_CB_DOCS)
        if indicator in ("party_support", "gov_approval"):
            docs = self._list_docs("poll", iso, RELATED_POLLS)
            docs.extend(self._list_docs("poll_of_polls", iso, 1))
            return docs
        if indicator in ("elec_mix", "energy_import_dep") or indicator.startswith("fuel_dep_"):
            return self._list_docs("energy_policy", iso, 1)
        return []

    # ------------------------------------------------------ GET /macro/docs

    def _docs(self, query):
        doc_type = str(query.get("type") or "").strip()
        if doc_type not in DOC_TYPES:
            return _err(400, f"type은 {', '.join(DOC_TYPES)} 중 하나여야 합니다.")
        iso = str(query.get("iso") or "").strip().upper()
        if not _DOC_ISO_RE.match(iso):
            return _err(400, "iso 파라미터가 필요합니다 (예: KR, 주간 브리프는 G20).")
        limit, error = _parse_limit(query.get("limit"), DEFAULT_DOC_LIMIT, MAX_DOC_LIMIT)
        if error:
            return error
        return _ok({"type": doc_type, "iso": iso, "docs": self._list_docs(doc_type, iso, limit)})

    # -------------------------------------------- GET /macro/politics/{iso}

    def _politics(self, iso_raw, query):
        iso = str(iso_raw or "").strip().upper()
        if not _ISO_RE.match(iso):
            return _err(400, "국가 코드는 2자리 ISO 코드여야 합니다 (예: KR).")
        raw_from = str(query.get("from") or "").strip()
        if raw_from:
            if not _DATE_RE.match(raw_from):
                return _err(400, "from은 YYYY-MM 형식이어야 합니다 (예: 2022-01).")
            from_month = raw_from[:7]
            if not _valid_period("M", from_month):
                return _err(400, "from은 YYYY-MM 형식이어야 합니다 (예: 2022-01).")
        else:
            from_month = self._months_ago(POLITICS_DEFAULT_MONTHS)

        election = self._list_docs("election", iso, 1)
        poll_of_polls = self._list_docs("poll_of_polls", iso, 1)
        not_applicable = self._list_docs("not_applicable", iso, 1)
        body = {
            "iso": iso,
            "from": from_month,
            "election": election[0] if election else None,
            "poll_of_polls": poll_of_polls[0] if poll_of_polls else None,
            "polls": self._list_docs("poll", iso, POLITICS_POLLS),
            "series": self._party_series(iso, from_month),
        }
        if not_applicable:
            body["not_applicable"] = not_applicable[0]
        return _ok(body)

    def _months_ago(self, months):
        now = self._now()
        total = now.year * 12 + (now.month - 1) - int(months)
        return f"{total // 12:04d}-{total % 12 + 1:02d}"

    def _party_series(self, iso, from_month):
        """주간 여론조사(payload.results)에서 정당별 월평균 지지율 시계열을 만든다."""
        cond = (Key("pk").eq(f"OBS#party_support#{iso}")
                & Key("sk").between(f"W#{from_month}", f"W#{SK_MAX}"))
        sums = {}
        for item in self._query_all(cond):
            row = _plain(item)
            results = (row.get("payload") or {}).get("results")
            month = str(row.get("period") or "")[:7]
            if not isinstance(results, dict) or not _valid_period("M", month):
                continue
            for party, pct in results.items():
                value = _num(pct)
                if value is None:
                    continue
                bucket = sums.setdefault(str(party), {}).setdefault(month, [0.0, 0])
                bucket[0] += float(value)
                bucket[1] += 1
        return {
            party: [_point(m, _round(months[m][0] / months[m][1])) for m in sorted(months)]
            for party, months in sums.items()
        }

    # -------------------------------------------------------- GET /macro/fx

    def _fx(self, query):
        freq = str(query.get("freq") or "M").strip().upper()
        if freq not in ("M", "D"):
            return _err(400, "freq는 M 또는 D여야 합니다 (통화가치는 월·일 단위만).")
        raw_base = str(query.get("base") or "").strip()
        if raw_base:
            if not _DATE_RE.match(raw_base):
                return _err(400, "base는 YYYY-MM-DD 형식이어야 합니다 (예: 2025-01-01).")
            base = raw_base[:7] if freq == "M" else (
                raw_base if len(raw_base) == 10 else f"{raw_base}-01"
            )
        else:
            base = self._default_fx_base(freq)
        if not _valid_period(freq, base):
            return _err(400, "base는 YYYY-MM-DD 형식이어야 합니다 (예: 2025-01-01).")
        now = self._now()
        if _period_key(freq, base) > _period_key(freq, _current_period(freq, now)):
            return _err(400, "base는 현재보다 늦을 수 없습니다.")
        error = _check_span(freq, base, None, now)
        if error:
            return error
        group = str(query.get("group") or "G20").strip().upper()

        euro_members = self._euro_members()
        # 기준통화(US)와 유로 회원국은 제외하고, 유로존(EU)은 EUR 대표로 항상 포함한다
        isos, error = self._group_isos(group, exclude={"US", *euro_members})
        if error:
            return error
        if isos is None:
            # 국가 사전이 없으면 fx_usd LATEST에 있는 국가로 대체 (Query 1회)
            found = {str(i.get("sk") or "") for i in self._query_all(Key("pk").eq("LATEST#fx_usd"))}
            isos = sorted(i for i in found if i and i != "US" and i not in euro_members)
        if EURO_REF not in isos:
            isos.append(EURO_REF)

        series = {}
        change = {}
        base_used = {}
        for iso in isos:
            valued = [p for p in self._obs_points("fx_usd", iso, freq, base, None) if p.get("value")]
            if not valued:
                continue
            # 조회 창이 base부터이므로 첫 관측이 기준(기준 기간 관측이 없으면 최근접 이후)
            ref = valued[0]
            if len(valued) > MAX_POINTS_PER_COUNTRY:
                return _err(400, f"국가당 관측치는 최대 {MAX_POINTS_PER_COUNTRY:,}점까지 조회할 수 있습니다. "
                                 f"base를 최근으로 옮기거나 freq=M을 사용하세요.")
            index = [
                _point(p["period"], _round(ref["value"] / p["value"] * 100.0), p.get("flags"))
                for p in valued
            ]
            series[iso] = index
            base_used[iso] = ref["period"]
            change[iso] = _round(index[-1]["value"] - 100.0)
        dxy = [
            _point(p["period"], p["value"])
            for p in self._obs_points("dxy", "US", freq, base, None) if p.get("value") is not None
        ]
        return _ok({
            "base": base,
            # 국가별로 base 시점 관측이 없을 수 있어 실제 사용한 기준 기간을 함께 내려준다
            "base_used": base_used,
            "freq": freq,
            "group": group,
            "series": series,
            "change": change,
            "dxy": dxy,
        })

    def _default_fx_base(self, freq):
        base_date = (self._now() - timedelta(days=FX_DEFAULT_DAYS)).date().isoformat()
        return base_date[:7] if freq == "M" else base_date

    # ------------------------------------------ POST /admin/macro/refresh

    def _refresh(self, body):
        sources = body.get("sources")
        if isinstance(sources, str):
            sources = _csv(sources)
        if not isinstance(sources, list):
            return _err(400, "sources 배열이 필요합니다 (예: [\"yahoo\", \"bis\"]).")
        names = _dedup([str(s).strip() for s in sources if str(s).strip()])
        if not names:
            return _err(400, "sources 배열이 필요합니다 (예: [\"yahoo\", \"bis\"]).")
        unknown = [s for s in names if s not in SOURCE_NAMES]
        if unknown:
            return _err(400, f"알 수 없는 소스입니다: {', '.join(unknown[:3])} "
                             f"(가능: {', '.join(SOURCE_NAMES)})")
        countries = body.get("countries")
        if isinstance(countries, str):
            countries = _csv(countries)
        isos = []
        if countries:
            if not isinstance(countries, list):
                return _err(400, "countries는 국가 코드 배열이어야 합니다.")
            for raw in countries:
                iso = str(raw).strip().upper()
                if not _ISO_RE.match(iso):
                    return _err(400, f"국가 코드가 올바르지 않습니다: {raw}")
                if iso not in isos:
                    isos.append(iso)
            if len(isos) > MAX_COUNTRIES:
                return _err(400, f"국가는 최대 {MAX_COUNTRIES}개까지 지정할 수 있습니다.")
        if self.run_task is None:
            return _err(503, "수집 태스크 실행이 구성되지 않았습니다.")
        lock = self._active_lock()
        if lock:
            return _err(409, "이미 수집이 실행 중입니다. 진행 중인 수집이 끝난 뒤 다시 시도하세요 "
                             f"(시작 {lock.get('acquired_at') or '?'} · 만료 {lock.get('expires_at') or '?'}).")

        command = ["python", "webui/macro/collect.py", "--sources", ",".join(names)]
        # 수동 재수집은 케이던스를 무시하는 것이 기본 동작 (force=false면 케이던스 준수)
        if body.get("force", True):
            command.append("--force")
        if isos:
            command.extend(["--countries", ",".join(isos)])
        try:
            task_arn = self.run_task(command)
        except Exception as e:  # noqa: BLE001 - RunTask 실패는 로그에만 상세를 남기고 고정 문구로 답한다
            print(f"macro refresh failed: {type(e).__name__}: {e}")
            return _err(500, "수집 태스크를 시작하지 못했습니다. 잠시 후 다시 시도하거나 워커 로그를 확인하세요.")
        return _ok({"task_arn": task_arn, "sources": names, "countries": isos,
                    "command": command})

    def _active_lock(self):
        """만료 전인 수집 락(`CONFIG / LOCK#collect`)이 있으면 그 항목, 없으면 None."""
        item = self._get(LOCK_PK, LOCK_SK)
        if not item:
            return None
        row = _plain(item)
        expires = str(row.get("expires_at") or "")
        if not expires:
            return None
        return row if expires > self._now().isoformat(timespec="seconds") else None

    # ------------------------------------------- POST /admin/macro/review

    def _review(self, body, actor):
        pk = str(body.get("pk") or "").strip()
        sk = str(body.get("sk") or "").strip()
        if not pk.startswith("DOC#") or not sk:
            return _err(400, "pk는 DOC#으로 시작해야 하며 sk가 필요합니다.")
        action = str(body.get("action") or "").strip().lower()
        if action not in ("approve", "reject"):
            return _err(400, "action은 approve 또는 reject여야 합니다.")
        note = body.get("note")
        now = self._now().isoformat(timespec="seconds")
        sets = {
            "review_status": "approved" if action == "approve" else "rejected",
            "reviewed_by": str(actor or "unknown"),
            "reviewed_at": now,
            "updated_at": now,
        }
        if note:
            sets["review_note"] = str(note)[:1000]
        names = {f"#k{i}": k for i, k in enumerate(sets)}
        values = {f":v{i}": v for i, v in enumerate(sets.values())}
        try:
            resp = self.table.update_item(
                Key={"pk": pk, "sk": sk},
                UpdateExpression="SET " + ", ".join(f"#k{i} = :v{i}" for i in range(len(sets))),
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ConditionExpression=Attr("pk").exists(),
                ReturnValues="ALL_NEW",
            )
        except ClientError as e:
            if str(e.response.get("Error", {}).get("Code", "")) == "ConditionalCheckFailedException":
                return _err(404, "해당 문서를 찾을 수 없습니다.")
            raise
        return _ok({"doc": _plain(resp.get("Attributes") or {})})
