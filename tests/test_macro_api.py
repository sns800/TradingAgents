# ============================================================
# [테스트 개요] G20 매크로 대시보드 API (webui/backend/macro_api.py + api_handler 후크)
#
# 실제 저장 경로(webui/macro/store.py)로 인메모리 테이블(FakeTable)을 채운 뒤,
# api_handler.py를 boto3 모킹 상태로 로드해 /api/macro/* 를 HTTP 계층까지 검증한다.
#  - meta: 국가 20 · 지표 38 · 소스별 수집 상태(errors_count) · 5분 컨테이너 캐시
#  - overview: 기본 9지표·freq/period·중앙값·국가군 필터·유로 회원국 EU 복제
#  - countries: KR 스냅샷 / DE 유로 병합(euro_ref) / 스냅샷 없는 국가 LATEST 폴백
#  - series: level·yoy·index100 수치, 유로 복제(euro_shared), 제한(30년·freq·형식)
#  - observations: 개정 이력·유로 대체(resolved_iso)·관련 문서·404
#  - docs / politics(월평균·not_applicable) / fx(지수·change·dxy·base_used)
#  - 관리자: refresh(RunTask command 인자·화이트리스트·403·수집 락 409·고정 오류 문구), review(상태·actor)
#  - 기간 상한: from 생략 시 30년(D 10년) 기본 시작·국가당 포인트 상한, fx base 상한
#  - 게이트: 인증 없음 401, MACRO_TABLE_NAME 없음 503
#
# tests/test_catalog_api.py·test_admin_api.py의 모킹 패턴과
# tests/test_macro_store.py의 sys.path 패턴을 함께 따른다.
# ============================================================
import base64
import importlib.util
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# boto3/botocore는 웹 UI 전용 의존성이라 순수 dev 설치(CI)에는 없을 수 있다.
pytest.importorskip("boto3", reason="webui deps not installed")
pytest.importorskip("yaml", reason="registry.yaml 로드에 PyYAML 필요")

ROOT = Path(__file__).resolve().parents[1]
HANDLER_PATH = ROOT / "webui" / "backend" / "api_handler.py"
sys.path.insert(0, str(ROOT / "webui"))  # macro.* (테스트 데이터 적재용)
sys.path.insert(0, str(ROOT / "webui" / "backend"))  # macro_api (api_handler와 같은 디렉터리)

import macro_api  # noqa: E402
from macro.fakeddb import FakeTable  # noqa: E402
from macro.registry import load_registry  # noqa: E402
from macro.schema import Doc, Observation  # noqa: E402
from macro.store import MacroStore  # noqa: E402

_ENV = {
    "TABLE_NAME": "runs-table",
    "DATA_BUCKET": "data-bucket",
    "CLUSTER_ARN": "arn:aws:ecs:cluster/test",
    "TASK_DEF": "worker-task",
    "SUBNET_IDS": "subnet-1,subnet-2",
    "SECURITY_GROUP": "sg-1",
    "COGNITO_CLIENT_ID": "client-id",
    "MACRO_TABLE_NAME": "tradingagents-webui-macro",
}

NOW = datetime(2026, 9, 20, 6, 0, tzinfo=timezone.utc)
NOW_ISO = "2026-09-20T06:00:00+00:00"
TASK_ARN = "arn:aws:ecs:ap-northeast-2:1:task/macro-1"
ADMIN_EMAIL = "admin@example.com"

# 시드 문서 키 (검토 API 테스트에서 sk를 알아야 한다)
CB_KR_DATE, CB_KR_ID = "2026-08-22", "aaaa11112222"


def _make_token(claims):
    """서명 검증 없이 페이로드만 디코드되는 JWT 형태의 토큰 (test_admin_api.py와 동일)."""
    def _b64(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return _b64({"alg": "none"}) + "." + _b64(claims) + ".sig"


_ADMIN_TOKEN = _make_token({
    "token_use": "access", "client_id": "client-id", "username": "admin-user",
    "email": ADMIN_EMAIL, "cognito:groups": ["admins"],
})
_USER_TOKEN = _make_token({
    "token_use": "access", "client_id": "client-id", "username": "plain-user",
    "cognito:groups": [],
})


# ---------------- 테스트 데이터 (실제 저장 경로로 적재) ----------------


def _obs(indicator, iso, freq, period, value, unit="%", source="bis", payload=None, flags=None):
    return Observation(
        indicator=indicator,
        iso=iso,
        freq=freq,
        period=period,
        value=value,
        payload=payload,
        unit=unit,
        source=source,
        series_id=f"TEST/{indicator}.{iso}",
        source_url=f"https://example.org/{indicator}/{iso}",
        method="테스트 시드 데이터",
        vintage="2026-09-01",
        retrieved_at=NOW_ISO,
        flags=list(flags or []),
    )


def _doc(doc_type, iso, date, doc_id, payload, **kw):
    kw.setdefault("title_ko", f"{iso} {doc_type} 문서")
    kw.setdefault("summary_ko", "요약 본문")
    kw.setdefault("source_url", f"https://example.org/{doc_type}/{iso}")
    kw.setdefault("source_name", "테스트 출처")
    return Doc(type=doc_type, iso=iso, date=date, id=doc_id, payload=payload, **kw)


def _observations():
    obs = [
        # 정책금리(월) — EU는 유로 회원국(DE/FR/IT)이 참조한다
        _obs("policy_rate", "KR", "M", "2026-06", 3.0),
        _obs("policy_rate", "KR", "M", "2026-07", 3.25),
        _obs("policy_rate", "KR", "M", "2026-08", 3.5),
        _obs("policy_rate", "US", "M", "2026-07", 5.0),
        _obs("policy_rate", "US", "M", "2026-08", 4.75),
        _obs("policy_rate", "JP", "M", "2026-08", 0.5),
        _obs("policy_rate", "EU", "M", "2026-07", 2.5),
        _obs("policy_rate", "EU", "M", "2026-08", 2.25),
        # 소비자물가 전년비(월)
        _obs("cpi_yoy", "KR", "M", "2026-08", 2.1, source="oecd"),
        _obs("cpi_yoy", "US", "M", "2026-08", 3.4, source="oecd"),
        _obs("cpi_yoy", "JP", "M", "2026-08", 1.2, source="oecd"),
        _obs("cpi_yoy", "DE", "M", "2026-08", 2.6, source="oecd"),
        _obs("cpi_yoy", "EU", "M", "2026-08", 2.4, source="oecd"),
        # 소비자물가 지수(월) — yoy·index100 수치 검증용 (1년 간격 3점)
        _obs("cpi_index", "KR", "M", "2024-08", 100, unit="index", source="oecd"),
        _obs("cpi_index", "KR", "M", "2025-08", 110, unit="index", source="oecd"),
        _obs("cpi_index", "KR", "M", "2026-08", 121, unit="index", source="oecd"),
        _obs("cpi_index", "US", "M", "2025-08", 200, unit="index", source="oecd"),
        _obs("cpi_index", "US", "M", "2026-08", 210, unit="index", source="oecd"),
        # 실질성장률(연)
        _obs("gdp_growth", "KR", "Y", "2025", 2.2, source="worldbank"),
        _obs("gdp_growth", "US", "Y", "2025", 2.8, source="worldbank"),
        _obs("gdp_growth", "DE", "Y", "2025", 0.5, source="worldbank"),
        # 국방비/정부지출(연)
        _obs("mil_expenditure_share", "KR", "Y", "2025", 12.0, source="worldbank"),
        # 환율(월) — 통화가치 지수 계산용. US는 기준통화라 지수에서 제외된다
        _obs("fx_usd", "KR", "M", "2025-01", 1300, unit="lcu_per_usd", source="yahoo"),
        _obs("fx_usd", "KR", "M", "2025-07", 1625, unit="lcu_per_usd", source="yahoo"),
        _obs("fx_usd", "KR", "M", "2026-08", 1040, unit="lcu_per_usd", source="yahoo"),
        _obs("fx_usd", "JP", "M", "2025-07", 150, unit="lcu_per_usd", source="yahoo"),
        _obs("fx_usd", "JP", "M", "2026-08", 120, unit="lcu_per_usd", source="yahoo"),
        _obs("fx_usd", "EU", "M", "2025-01", 0.9, unit="lcu_per_usd", source="yahoo"),
        _obs("fx_usd", "EU", "M", "2026-08", 0.9, unit="lcu_per_usd", source="yahoo"),
        _obs("fx_usd", "US", "M", "2025-01", 1.0, unit="lcu_per_usd", source="yahoo"),
        _obs("dxy", "US", "M", "2025-01", 100, unit="index", source="fred"),
        _obs("dxy", "US", "M", "2026-08", 95, unit="index", source="fred"),
        # 정당 지지율: 주간 원자료(payload.results) + 월 집계(개요·드로어용)
        _obs("party_support", "KR", "W", "2026-07-10", 33.0, source="wiki_polls",
             payload={"results": {"여당": 33.0, "야당": 41.0}}),
        _obs("party_support", "KR", "W", "2026-07-24", 35.0, source="wiki_polls",
             payload={"results": {"여당": 35.0, "야당": 39.0}}),
        _obs("party_support", "KR", "W", "2026-08-14", 37.0, source="wiki_polls",
             payload={"results": {"여당": 37.0, "야당": 38.0}}),
        _obs("party_support", "KR", "M", "2026-08", 37.0, source="wiki_polls",
             payload={"results": {"여당": 37.0, "야당": 38.0},
                      "ruling_party": "여당", "leader_party": "야당"}),
        _obs("party_support", "US", "M", "2026-08", 44.0, source="wiki_polls",
             payload={"results": {"여당": 44.0, "야당": 46.0}}),
        _obs("gov_approval", "KR", "M", "2026-08", 42.0, source="wiki_polls"),
        # 복합값(payload) 지표
        _obs("elec_mix", "KR", "Y", "2025", None, unit="pct_share", source="ember",
             payload={"items": [{"label": "석탄", "value": 29.1},
                                {"label": "원자력", "value": 31.5}]}),
    ]
    return obs


def _docs():
    return [
        _doc("cb_stance", "KR", CB_KR_DATE, CB_KR_ID,
             {"stance_score": 0.5, "direction": "hold", "forward_guidance": "neutral",
              "rate_after": 3.5, "statement_date": CB_KR_DATE, "meeting_type": "정기"},
             ai_generated=True, model_id="test-model", confidence=0.8,
             quotes=["물가 안정 기조를 유지한다"], review_status="pending"),
        _doc("cb_stance", "KR", "2026-07-11", "aaaa11113333",
             {"stance_score": 1.0, "direction": "hike", "forward_guidance": "tightening"}),
        _doc("cb_stance", "EU", "2026-09-05", "bbbb11112222",
             {"stance_score": -0.5, "direction": "cut", "forward_guidance": "easing",
              "rate_after": 2.25}),
        _doc("poll", "KR", "2026-07-10", "cccc11110001",
             {"pollster": "리얼미터", "fieldwork_end": "2026-07-10", "sample_size": 1000,
              "results": {"여당": 33.0, "야당": 41.0}, "gov_approval": 39.0}),
        _doc("poll", "KR", "2026-07-24", "cccc11110002",
             {"pollster": "갤럽", "fieldwork_end": "2026-07-24", "sample_size": 1004,
              "results": {"여당": 35.0, "야당": 39.0}, "gov_approval": 41.0}),
        _doc("poll", "KR", "2026-08-14", "cccc11110003",
             {"pollster": "NBS", "fieldwork_end": "2026-08-14", "sample_size": 1002,
              "results": {"여당": 37.0, "야당": 38.0}, "gov_approval": 42.0}),
        _doc("poll_of_polls", "KR", "2026-08-20", "dddd11110001",
             {"asof": "2026-08-20", "window_days": 30, "results": {"여당": 36.0, "야당": 38.5},
              "n_polls": 5, "ruling_party": "여당", "ruling_pct": 36.0,
              "leader_party": "야당", "leader_pct": 38.5, "gov_approval": 42.0}),
        _doc("election", "KR", "2026-01-05", "eeee11110001",
             {"next_election_date": "2028-04-12", "election_type": "총선",
              "ruling_party": "여당", "ruling_lean": "중도"}),
        _doc("energy_policy", "KR", "2026-03-02", "ffff11110001",
             {"targets": ["2030 NDC 40%"], "recent_changes": ["11차 전기본"],
              "sources": ["https://example.org/energy"]}),
        _doc("weekly_brief", "G20", "2026-09-14", "999911110001",
             {"week_start": "2026-09-14", "bullets": ["ECB 동결"], "evidence": []}),
        _doc("not_applicable", "CN", "2026-01-01", "888811110001",
             {"reason": "경쟁 정당이 없는 체제"}),
    ]


def _seed(table):
    """수집기와 동일한 경로(MacroStore)로 테이블을 채운다."""
    store = MacroStore(table, now=lambda: NOW_ISO)
    store.put_meta_items(load_registry().to_meta_items())

    observations = _observations()
    # 개정 이력(revisions) 시드: 같은 키를 먼저 다른 값으로 저장해 둔다
    store.put_observations([_obs("policy_rate", "KR", "M", "2026-08", 3.4)])
    store.put_observations(observations)
    store.put_docs(_docs())

    pairs = {}
    for o in observations:
        pairs.setdefault(o.indicator, set()).add(o.iso)
    for indicator, isos in pairs.items():
        store.rebuild_latest(indicator, sorted(isos))
    # 스냅샷은 KR·DE·EU만 만든다 (JP는 스냅샷 없는 폴백 경로 검증용)
    for iso in ("KR", "DE", "EU"):
        store.rebuild_snapshot(iso, macro_api.INDICATOR_IDS)

    store.log_ingest("bis", {"status": "ok", "n_obs": 120, "n_docs": 0,
                             "cadence_days": 1, "errors": []})
    store.log_ingest("yahoo", {"status": "ok", "n_obs": 40, "cadence_days": 1, "errors": []})
    store.log_ingest("oecd", {"status": "partial", "n_obs": 10, "cadence_days": 7,
                              "errors": ["KR cpi 실패"]})
    return store


# ---------------- 픽스처 / 호출 헬퍼 ----------------


@pytest.fixture()
def table():
    t = FakeTable()
    _seed(t)
    t.calls.clear()
    return t


@pytest.fixture()
def api(monkeypatch, table):
    """api_handler를 boto3 모킹 상태로 로드하고 매크로 테이블을 페이크로 바꿔 끼운다."""
    for key, value in _ENV.items():
        monkeypatch.setenv(key, value)
    with patch("boto3.resource", return_value=MagicMock()), \
         patch("boto3.client", return_value=MagicMock()):
        spec = importlib.util.spec_from_file_location("webui_macro_handler_under_test",
                                                      HANDLER_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

    # 인증: 토큰 캐시를 미리 채워 Cognito 호출 없이 통과시킨다
    mod._token_cache[_ADMIN_TOKEN] = time.time() + 300
    mod._token_cache[_USER_TOKEN] = time.time() + 300

    mod.table = MagicMock()
    mod.s3 = MagicMock()
    mod.dynamodb = MagicMock()
    mod.dynamodb.Table.return_value = table
    mod.ecs = MagicMock()
    mod.ecs.run_task.return_value = {"failures": [], "tasks": [{"taskArn": TASK_ARN}]}
    return mod


def _event(method, path, query=None, body=None, token=_USER_TOKEN):
    return {
        "requestContext": {"http": {"method": method}},
        "rawPath": path,
        "queryStringParameters": query,
        "headers": {"x-access-token": token} if token else {},
        "body": json.dumps(body, ensure_ascii=False) if body is not None else None,
    }


def _call(api, method, path, query=None, body=None, token=_USER_TOKEN):
    """응답을 (status, body) 로 돌려준다. body가 JSON으로 파싱되는지도 함께 검증한다."""
    res = api.handler(_event(method, path, query=query, body=body, token=token), None)
    assert res["headers"]["content-type"].startswith("application/json")
    return res["statusCode"], json.loads(res["body"])


def _get(api, path, query=None):
    status, body = _call(api, "GET", path, query=query)
    assert status == 200, body
    assert body["ok"] is True
    return body


def _cells(body, iso):
    for row in body["rows"]:
        if row["iso"] == iso:
            return row["cells"]
    raise AssertionError(f"{iso} 행이 없습니다: {[r['iso'] for r in body['rows']]}")


# ---------------- GET /api/macro/meta ----------------


def test_meta_returns_countries_indicators_and_ingest(api):
    body = _get(api, "/api/macro/meta")
    assert [c["iso"] for c in body["countries"]][:5] == ["KR", "US", "JP", "CN", "EU"]
    assert len(body["countries"]) == 20
    kr = body["countries"][0]
    assert kr["name_ko"] == "한국" and kr["ccy"] == "KRW" and kr["euro"] is False
    assert "G20" in kr["groups"]
    de = next(c for c in body["countries"] if c["iso"] == "DE")
    assert de["euro"] is True

    assert [i["id"] for i in body["indicators"]] == list(macro_api.INDICATOR_IDS)
    assert len(body["indicators"]) == 38
    policy = body["indicators"][0]
    assert policy["name_ko"] == "정책금리"
    assert policy["unit"] == "%" and policy["category"] == "monetary"
    assert policy["native_freq"] == "D" and "M" in policy["store_freqs"]
    assert policy["agg"] == "last" and policy["decimals"] == 2
    assert policy["composite"] is False
    assert next(i for i in body["indicators"] if i["id"] == "elec_mix")["composite"] is True

    # 기록이 없는 소스는 응답에서 빠진다 (프론트의 "소스 n/m 정상" 계산 보호)
    assert sorted(body["ingest"]) == ["bis", "oecd", "yahoo"]
    assert body["ingest"]["bis"]["status"] == "ok"
    assert body["ingest"]["bis"]["n_obs"] == 120
    assert body["ingest"]["bis"]["finished_at"] == NOW_ISO
    # next_due는 날짜 경계(finished_at 날짜 + cadence_days의 00:00 UTC) — CONTRACT 12장
    assert body["ingest"]["bis"]["next_due"] == "2026-09-21T00:00:00+00:00"
    assert body["ingest"]["oecd"]["status"] == "partial"
    assert body["ingest"]["oecd"]["errors_count"] == 1


def test_meta_is_cached_in_container(api, table):
    _get(api, "/api/macro/meta")
    before = dict(table.calls)
    assert before.get("get_item", 0) >= 38 + 1 + 3  # 지표 38 + 국가 1 + 수집 로그
    _get(api, "/api/macro/meta")
    assert dict(table.calls) == before  # 5분 캐시라 두 번째 호출은 DynamoDB를 건드리지 않는다


# ---------------- GET /api/macro/overview ----------------


def test_overview_defaults_cells_and_asof(api):
    body = _get(api, "/api/macro/overview")
    assert [i["id"] for i in body["indicators"]] == list(macro_api.DEFAULT_OVERVIEW_INDICATORS)
    assert body["indicators"][0]["name_ko"] == "정책금리"  # 객체 배열(메타 포함)
    assert body["asof"] == {"M": "2026-08", "Q": "2026-Q2", "Y": "2025"}
    assert len(body["rows"]) == 20 and body["group"] == "G20"

    kr = _cells(body, "KR")
    assert kr["policy_rate"]["value"] == 3.5
    # 프론트 드로어가 freq·period로 관측치를 다시 조회하므로 둘 다 필수
    assert kr["policy_rate"]["period"] == "2026-08" and kr["policy_rate"]["freq"] == "M"
    assert kr["policy_rate"]["change"] == 0.25
    assert kr["policy_rate"]["rank"] == 2 and kr["policy_rate"]["unit"] == "%"
    assert kr["policy_rate"]["source"] == "bis"
    assert kr["gdp_growth"]["period"] == "2025" and kr["gdp_growth"]["freq"] == "Y"
    # 복합/정치 지표의 payload는 그대로 내려준다 (집권당·1위 정당 표기용)
    assert kr["party_support"]["payload"]["leader_party"] == "야당"


def test_overview_euro_members_reuse_eu_values(api):
    body = _get(api, "/api/macro/overview")
    for iso in ("DE", "FR", "IT"):
        cell = _cells(body, iso)["policy_rate"]
        assert cell["value"] == 2.25 and cell["period"] == "2026-08"
        assert "euro_area_shared" in cell["flags"]
    # 유로 공유 지표가 아닌 값은 회원국 자체 관측을 쓴다
    de_cpi = _cells(body, "DE")["cpi_yoy"]
    assert de_cpi["value"] == 2.6 and de_cpi["flags"] == []


def test_overview_median_and_group_filter(api):
    body = _get(api, "/api/macro/overview")
    # 표시되는 값: KR 3.5 · US 4.75 · JP 0.5 · EU 2.25 · DE/FR/IT 2.25(유로 공유)
    assert body["median"]["policy_rate"] == 2.25
    assert body["median"]["cpi_yoy"] == 2.4
    assert body["median"]["house_price_yoy"] is None  # 값이 없는 지표는 None

    g7 = _get(api, "/api/macro/overview", {"group": "G7"})
    assert [r["iso"] for r in g7["rows"]] == ["US", "JP", "DE", "FR", "IT", "GB", "CA"]
    assert g7["median"]["policy_rate"] == 2.25  # US 4.75 · JP 0.5 · DE/FR/IT 2.25

    status, body = _call(api, "GET", "/api/macro/overview", {"group": "OECD"})
    assert status == 400 and "국가군" in body["error"]


def test_overview_explicit_indicators_and_whitelist(api):
    body = _get(api, "/api/macro/overview",
                {"group": "G20", "indicators": "party_support,gov_approval"})
    assert [i["id"] for i in body["indicators"]] == ["party_support", "gov_approval"]
    assert _cells(body, "KR")["gov_approval"]["value"] == 42.0

    status, body = _call(api, "GET", "/api/macro/overview", {"indicators": "policy_rate,bogus"})
    assert status == 400 and "bogus" in body["error"]


# ---------------- GET /api/macro/countries/{iso} ----------------


def test_country_profile_from_snapshot(api):
    body = _get(api, "/api/macro/countries/KR")
    assert body["country"]["name_ko"] == "한국" and body["profile_source"] == "snapshot"
    assert "euro_ref" not in body
    assert body["latest"]["policy_rate"]["value"] == 3.5
    assert body["latest"]["policy_rate"]["period"] == "2026-08"
    assert body["latest"]["elec_mix"]["payload"]["items"][0]["label"] == "석탄"
    docs = body["docs"]
    assert docs["cb_stance"]["payload"]["stance_score"] == 0.5
    assert docs["cb_stance"]["review_status"] == "pending"  # AI 판정은 검토 전에도 노출
    # 검토 API 호출용 키가 문서 요약에 포함된다 (프론트 [승인]/[반려] 버튼)
    assert docs["cb_stance"]["pk"] == "DOC#cb_stance#KR"
    assert docs["cb_stance"]["sk"] == f"{CB_KR_DATE}#{CB_KR_ID}"
    assert docs["election"]["payload"]["next_election_date"] == "2028-04-12"
    assert docs["poll_of_polls"]["payload"]["leader_party"] == "야당"


def test_country_profile_euro_member_merges_eu(api):
    body = _get(api, "/api/macro/countries/DE")
    assert body["euro_ref"] == "EU"
    shared = body["latest"]["policy_rate"]
    assert shared["value"] == 2.25 and shared["iso"] == "DE"
    assert "euro_area_shared" in shared["flags"] and shared["euro_ref"] == "EU"
    assert body["latest"]["cpi_yoy"]["value"] == 2.6  # 자체 관측은 그대로
    assert body["docs"]["cb_stance"]["iso"] == "EU"  # ECB 결정문으로 보강


def test_country_profile_falls_back_without_snapshot(api):
    body = _get(api, "/api/macro/countries/JP")
    assert body["profile_source"] == "fallback"
    assert body["latest"]["policy_rate"]["value"] == 0.5
    assert body["latest"]["cpi_yoy"]["value"] == 1.2
    assert body["docs"] == {}


def test_country_profile_rejects_bad_iso(api):
    status, body = _call(api, "GET", "/api/macro/countries/KOR")
    assert status == 400 and "ISO" in body["error"]


# ---------------- GET /api/macro/series ----------------


def test_series_level(api):
    body = _get(api, "/api/macro/series", {
        "indicator": "policy_rate", "countries": "KR,US", "freq": "M",
        "from": "2026-01", "to": "2026-12",
    })
    assert body["indicator"]["name_ko"] == "정책금리"
    assert body["freq"] == "M" and body["transform"] == "level"
    assert body["series"]["KR"] == [
        {"period": "2026-06", "value": 3.0},
        {"period": "2026-07", "value": 3.25},
        {"period": "2026-08", "value": 3.5},
    ]
    assert [p["period"] for p in body["series"]["US"]] == ["2026-07", "2026-08"]
    assert body["euro_shared"] == []


def test_series_yoy_is_computed_from_index(api):
    body = _get(api, "/api/macro/series", {
        "indicator": "cpi_index", "countries": "KR", "freq": "M",
        "from": "2025-01", "to": "2026-12", "transform": "yoy",
    })
    # 12개월 전 값 대비: 110/100 → +10%, 121/110 → +10%. 창을 1년 앞으로 늘려 계산한다
    assert body["series"]["KR"] == [
        {"period": "2025-08", "value": 10.0},
        {"period": "2026-08", "value": 10.0},
    ]


def test_series_index100_uses_base_period(api):
    body = _get(api, "/api/macro/series", {
        "indicator": "cpi_index", "countries": "KR", "freq": "M",
        "from": "2024-01", "to": "2026-12", "transform": "index100", "base": "2024-08",
    })
    assert body["base"] == "2024-08" and body["base_used"] == {"KR": "2024-08"}
    assert body["series"]["KR"] == [
        {"period": "2024-08", "value": 100.0},
        {"period": "2025-08", "value": 110.0},
        {"period": "2026-08", "value": 121.0},
    ]

    # 기준 기간이 조회 시작보다 이르면 기준값을 위해 창을 늘리고 결과만 자른다
    body = _get(api, "/api/macro/series", {
        "indicator": "cpi_index", "countries": "KR", "freq": "M",
        "from": "2025-01", "transform": "index100", "base": "2024-08",
    })
    assert [p["period"] for p in body["series"]["KR"]] == ["2025-08", "2026-08"]
    assert body["series"]["KR"][0]["value"] == 110.0

    # 기준 기간 이전 관측이 없으면 가장 이른 관측을 기준으로 쓴다 (base_used로 알린다)
    body = _get(api, "/api/macro/series", {
        "indicator": "cpi_index", "countries": "US", "freq": "M",
        "transform": "index100", "base": "2020-01",
    })
    assert body["base_used"] == {"US": "2025-08"}
    assert body["series"]["US"][0]["value"] == 100.0


def test_series_duplicates_eu_for_euro_members(api):
    body = _get(api, "/api/macro/series", {
        "indicator": "policy_rate", "countries": "DE,FR,KR", "freq": "M", "from": "2026-01",
    })
    assert body["euro_shared"] == ["DE", "FR"]
    assert [p["value"] for p in body["series"]["DE"]] == [2.5, 2.25]
    assert body["series"]["DE"] == body["series"]["FR"]
    assert body["series"]["DE"][0]["flags"] == ["euro_area_shared"]
    assert "flags" not in body["series"]["KR"][0]  # 빈 flags는 응답에서 생략


def test_series_rejects_invalid_parameters(api):
    base = {"indicator": "policy_rate", "countries": "KR", "freq": "M"}
    status, body = _call(api, "GET", "/api/macro/series", {**base, "freq": "X"})
    assert status == 400 and "freq" in body["error"]

    status, body = _call(api, "GET", "/api/macro/series", {**base, "from": "2026"})
    assert status == 400 and "YYYY-MM" in body["error"]

    status, body = _call(api, "GET", "/api/macro/series",
                         {**base, "from": "1990-01", "to": "2026-08"})
    assert status == 400 and "30년" in body["error"]

    status, body = _call(api, "GET", "/api/macro/series", {**base, "transform": "diff"})
    assert status == 400 and "transform" in body["error"]

    status, body = _call(api, "GET", "/api/macro/series",
                         {**base, "freq": "D", "transform": "yoy"})
    assert status == 400 and "전년비" in body["error"]

    status, body = _call(api, "GET", "/api/macro/series",
                         {**base, "countries": ",".join(["KR"] * 1)[:0]})
    assert status == 400 and "countries" in body["error"]

    many = ",".join(f"X{i}" for i in range(21))
    status, body = _call(api, "GET", "/api/macro/series", {**base, "countries": many})
    assert status == 400

    status, body = _call(api, "GET", "/api/macro/series", {**base, "indicator": "nope"})
    assert status == 400


def test_series_defaults_from_to_span_limit_and_caps_daily(table):
    """from 생략 → (to 또는 현재) − 30년이 기본 시작이고 상한 검사가 항상 적용된다. 일별은 10년."""
    api = macro_api.MacroApi(table=table, now=lambda: NOW)

    def call(query):
        res = api.handle("GET", "/api/macro/series", query)
        return res["statusCode"], json.loads(res["body"])

    status, body = call({"indicator": "policy_rate", "countries": "KR", "freq": "M"})
    assert status == 200 and body["from"] == "1996-09" and body["to"] is None
    assert [p["period"] for p in body["series"]["KR"]] == ["2026-06", "2026-07", "2026-08"]

    status, body = call({"indicator": "policy_rate", "countries": "KR", "freq": "M", "to": "2010-12"})
    assert status == 200 and body["from"] == "1980-12" and body["series"]["KR"] == []

    status, body = call({"indicator": "gdp_growth", "countries": "KR", "freq": "Y"})
    assert status == 200 and body["from"] == "1996"

    status, body = call({"indicator": "fx_usd", "countries": "KR", "freq": "D"})
    assert status == 200 and body["from"] == "2016-09-20"  # 일별 기본 시작 = 10년 전

    status, body = call({"indicator": "fx_usd", "countries": "KR", "freq": "D",
                         "from": "2010-01-01", "to": "2026-09-01"})
    assert status == 400 and "10년" in body["error"]

    status, body = call({"indicator": "fx_usd", "countries": "KR", "freq": "D",
                         "from": "2020-01-01", "to": "2026-09-01"})
    assert status == 200 and body["from"] == "2020-01-01"


def test_series_rejects_too_many_points_per_country(table, monkeypatch):
    api = macro_api.MacroApi(table=table, now=lambda: NOW)
    monkeypatch.setattr(macro_api, "MAX_POINTS_PER_COUNTRY", 2)
    res = api.handle("GET", "/api/macro/series",
                     {"indicator": "policy_rate", "countries": "KR", "freq": "M", "from": "2026-01"})
    body = json.loads(res["body"])
    assert res["statusCode"] == 400 and "4,000" not in body["error"] and "2점" in body["error"]
    res = api.handle("GET", "/api/macro/series",
                     {"indicator": "policy_rate", "countries": "KR", "freq": "M", "from": "2026-07"})
    assert res["statusCode"] == 200


def test_series_follows_query_pagination(table):
    """FakeTable의 페이지 크기를 줄여 LastEvaluatedKey 처리를 확인한다."""
    small = FakeTable(page_size=2)
    _seed(small)
    api = macro_api.MacroApi(table=small, now=lambda: NOW)
    res = api.handle("GET", "/api/macro/series", {
        "indicator": "policy_rate", "countries": "KR", "freq": "M", "from": "2026-01",
    })
    body = json.loads(res["body"])
    assert res["statusCode"] == 200
    assert [p["period"] for p in body["series"]["KR"]] == ["2026-06", "2026-07", "2026-08"]


# ---------------- GET /api/macro/observations ----------------


def test_observation_includes_revisions_and_related_docs(api):
    body = _get(api, "/api/macro/observations", {
        "indicator": "policy_rate", "iso": "KR", "freq": "M", "period": "2026-08",
    })
    obs = body["observation"]
    assert obs["value"] == 3.5 and obs["unit"] == "%" and obs["source"] == "bis"
    assert obs["series_id"] == "TEST/policy_rate.KR" and obs["vintage"] == "2026-09-01"
    assert obs["method"] == "테스트 시드 데이터"
    assert [r["value"] for r in obs["revisions"]] == [3.4]
    assert body["resolved_iso"] == "KR"
    assert [d["type"] for d in body["related_docs"]] == ["cb_stance", "cb_stance"]
    assert body["related_docs"][0]["date"] == CB_KR_DATE  # 최신순
    assert body["related_docs"][0]["pk"] == "DOC#cb_stance#KR"
    assert body["related_docs"][0]["sk"] == f"{CB_KR_DATE}#{CB_KR_ID}"


def test_observation_euro_member_falls_back_to_eu(api):
    body = _get(api, "/api/macro/observations", {
        "indicator": "policy_rate", "iso": "DE", "freq": "M", "period": "2026-08",
    })
    assert body["resolved_iso"] == "EU"
    assert body["observation"]["value"] == 2.25
    assert "euro_area_shared" in body["observation"]["flags"]
    assert [d["iso"] for d in body["related_docs"]] == ["EU"]


def test_observation_related_docs_by_category(api):
    polls = _get(api, "/api/macro/observations", {
        "indicator": "party_support", "iso": "KR", "freq": "M", "period": "2026-08",
    })["related_docs"]
    assert [d["type"] for d in polls] == ["poll", "poll", "poll", "poll_of_polls"]

    energy = _get(api, "/api/macro/observations", {
        "indicator": "elec_mix", "iso": "KR", "freq": "Y", "period": "2025",
    })
    assert energy["observation"]["value"] is None
    assert energy["observation"]["payload"]["items"][1]["value"] == 31.5
    assert [d["type"] for d in energy["related_docs"]] == ["energy_policy"]


def test_observation_missing_and_invalid(api):
    status, body = _call(api, "GET", "/api/macro/observations", {
        "indicator": "policy_rate", "iso": "BR", "freq": "M", "period": "2026-08",
    })
    assert status == 404 and body["error"] == "해당 관측치를 찾을 수 없습니다."

    status, body = _call(api, "GET", "/api/macro/observations", {
        "indicator": "policy_rate", "iso": "KR", "freq": "M", "period": "2026-08-01",
    })
    assert status == 400 and "period" in body["error"]


# ---------------- GET /api/macro/docs ----------------


def test_docs_list_newest_first_with_limit(api):
    body = _get(api, "/api/macro/docs", {"type": "cb_stance", "iso": "KR"})
    assert [d["date"] for d in body["docs"]] == [CB_KR_DATE, "2026-07-11"]
    assert body["docs"][0]["sk"] == f"{CB_KR_DATE}#{CB_KR_ID}"

    body = _get(api, "/api/macro/docs", {"type": "cb_stance", "iso": "KR", "limit": "1"})
    assert len(body["docs"]) == 1

    body = _get(api, "/api/macro/docs", {"type": "weekly_brief", "iso": "G20", "limit": "1"})
    assert body["docs"][0]["payload"]["bullets"] == ["ECB 동결"]


def test_docs_validates_type_and_limit(api):
    status, body = _call(api, "GET", "/api/macro/docs", {"type": "secret", "iso": "KR"})
    assert status == 400 and "type" in body["error"]

    status, body = _call(api, "GET", "/api/macro/docs", {"type": "poll"})
    assert status == 400 and "iso" in body["error"]

    for limit in ("0", "101", "abc"):
        status, body = _call(api, "GET", "/api/macro/docs",
                             {"type": "poll", "iso": "KR", "limit": limit})
        assert status == 400 and "limit" in body["error"]


# ---------------- GET /api/macro/politics/{iso} ----------------


def test_politics_monthly_party_averages(api):
    body = _get(api, "/api/macro/politics/KR", {"from": "2026-01"})
    assert body["from"] == "2026-01"
    assert body["polls"] and all(d["pk"] == "DOC#poll#KR" and d["sk"] for d in body["polls"])
    # 2026-07은 33.0·35.0 평균 = 34.0, 2026-08은 37.0 1건
    assert body["series"]["여당"] == [
        {"period": "2026-07", "value": 34.0},
        {"period": "2026-08", "value": 37.0},
    ]
    assert body["series"]["야당"] == [
        {"period": "2026-07", "value": 40.0},
        {"period": "2026-08", "value": 38.0},
    ]
    assert [d["payload"]["pollster"] for d in body["polls"]] == ["NBS", "갤럽", "리얼미터"]
    assert body["election"]["payload"]["election_type"] == "총선"
    assert body["poll_of_polls"]["payload"]["n_polls"] == 5
    assert "not_applicable" not in body

    # from 이후 구간만 집계한다
    body = _get(api, "/api/macro/politics/KR", {"from": "2026-08"})
    assert [p["period"] for p in body["series"]["여당"]] == ["2026-08"]


def test_politics_not_applicable_country(api):
    body = _get(api, "/api/macro/politics/CN")
    assert body["not_applicable"]["payload"]["reason"] == "경쟁 정당이 없는 체제"
    assert body["series"] == {} and body["polls"] == []
    assert body["election"] is None and body["poll_of_polls"] is None


def test_politics_rejects_bad_from(api):
    status, body = _call(api, "GET", "/api/macro/politics/KR", {"from": "2026/01"})
    assert status == 400 and "YYYY-MM" in body["error"]


# ---------------- GET /api/macro/fx ----------------


def test_fx_index_change_and_dxy(api):
    body = _get(api, "/api/macro/fx", {"base": "2025-01-01", "freq": "M", "group": "G20"})
    assert body["base"] == "2025-01" and body["freq"] == "M"
    # 통화가치 지수 = 기준환율 / 환율 × 100 (환율 하락 = 통화 강세 = 지수 상승)
    assert body["series"]["KR"] == [
        {"period": "2025-01", "value": 100.0},
        {"period": "2025-07", "value": 80.0},
        {"period": "2026-08", "value": 125.0},
    ]
    assert body["change"]["KR"] == 25.0
    assert body["base_used"]["KR"] == "2025-01"
    # 기준 기간 관측이 없으면 최근접 이후 첫 관측을 기준으로 쓴다
    assert body["base_used"]["JP"] == "2025-07"
    assert [p["value"] for p in body["series"]["JP"]] == [100.0, 125.0]
    assert body["change"]["EU"] == 0.0
    # 기준통화(US)와 유로 회원국은 제외, 유로존(EU)은 포함
    assert "US" not in body["series"] and "DE" not in body["series"]
    assert "EU" in body["series"]
    assert body["dxy"] == [
        {"period": "2025-01", "value": 100},
        {"period": "2026-08", "value": 95},
    ]


def test_fx_validates_freq_and_base(api):
    status, body = _call(api, "GET", "/api/macro/fx", {"freq": "Q"})
    assert status == 400 and "freq" in body["error"]

    status, body = _call(api, "GET", "/api/macro/fx", {"base": "2025/01/01"})
    assert status == 400 and "base" in body["error"]


def test_fx_base_is_capped_to_span_limit(table):
    """base부터 현재까지 30년 상한, 일별(D)은 10년. 미래 base도 거절."""
    api = macro_api.MacroApi(table=table, now=lambda: NOW)

    def call(query):
        res = api.handle("GET", "/api/macro/fx", query)
        return res["statusCode"], json.loads(res["body"])

    status, body = call({"base": "1900-01-01", "freq": "M"})
    assert status == 400 and "30년" in body["error"]
    status, body = call({"base": "2010-01-01", "freq": "D"})
    assert status == 400 and "10년" in body["error"]
    status, body = call({"base": "2030-01-01", "freq": "M"})
    assert status == 400 and "현재보다 늦을" in body["error"]
    status, body = call({"base": "1996-10-01", "freq": "M"})
    assert status == 200 and body["base"] == "1996-10"
    status, body = call({"base": "2017-01-01", "freq": "D"})
    assert status == 200 and body["base"] == "2017-01-01"


# ---------------- POST /api/admin/macro/* ----------------


def test_admin_refresh_starts_worker_task(api):
    status, body = _call(api, "POST", "/api/admin/macro/refresh", token=_ADMIN_TOKEN,
                         body={"sources": ["bis", "yahoo"], "countries": ["kr", "us"]})
    assert status == 200 and body["task_arn"] == TASK_ARN
    assert body["sources"] == ["bis", "yahoo"] and body["countries"] == ["KR", "US"]

    kwargs = api.ecs.run_task.call_args.kwargs
    assert kwargs["cluster"] == _ENV["CLUSTER_ARN"]
    assert kwargs["taskDefinition"] == _ENV["TASK_DEF"]
    vpc = kwargs["networkConfiguration"]["awsvpcConfiguration"]
    assert vpc["subnets"] == ["subnet-1", "subnet-2"]
    assert vpc["securityGroups"] == ["sg-1"]
    override = kwargs["overrides"]["containerOverrides"][0]
    assert override["name"] == "worker"
    assert override["command"] == [
        "python", "webui/macro/collect.py", "--sources", "bis,yahoo",
        "--force", "--countries", "KR,US",
    ]


def test_admin_refresh_force_false_omits_flag(api):
    status, body = _call(api, "POST", "/api/admin/macro/refresh", token=_ADMIN_TOKEN,
                         body={"sources": ["bis"], "force": False})
    assert status == 200
    assert body["command"] == ["python", "webui/macro/collect.py", "--sources", "bis"]


def test_admin_refresh_validates_input(api):
    status, body = _call(api, "POST", "/api/admin/macro/refresh", token=_ADMIN_TOKEN,
                         body={"sources": ["bis", "nasa"]})
    assert status == 400 and "nasa" in body["error"]
    assert api.ecs.run_task.call_count == 0

    status, body = _call(api, "POST", "/api/admin/macro/refresh", token=_ADMIN_TOKEN,
                         body={"sources": []})
    assert status == 400 and "sources" in body["error"]

    status, body = _call(api, "POST", "/api/admin/macro/refresh", token=_ADMIN_TOKEN,
                         body={"sources": ["bis"], "countries": ["KOR"]})
    assert status == 400 and "국가 코드" in body["error"]


def test_admin_refresh_requires_admin_and_reports_run_task_failure(api):
    status, body = _call(api, "POST", "/api/admin/macro/refresh", token=_USER_TOKEN,
                         body={"sources": ["bis"]})
    assert status == 403 and body["error"] == "관리자 권한이 필요합니다."

    api.ecs.run_task.return_value = {"failures": [{"reason": "capacity"}], "tasks": []}
    status, body = _call(api, "POST", "/api/admin/macro/refresh", token=_ADMIN_TOKEN,
                         body={"sources": ["bis"]})
    # 내부 예외 문자열은 로그에만 남기고 응답은 고정 한국어 문구
    assert status == 500 and body["error"].startswith("수집 태스크를 시작하지 못했습니다")
    assert "capacity" not in body["error"]


def test_admin_refresh_rejects_while_collect_lock_active(api, table):
    lock = {"pk": "CONFIG", "sk": "LOCK#collect", "holder": "run-1",
            "acquired_at": "2026-09-20T05:00:00+00:00", "expires_at": "2099-01-01T00:00:00+00:00"}
    table.put_item(Item=lock)
    status, body = _call(api, "POST", "/api/admin/macro/refresh", token=_ADMIN_TOKEN,
                         body={"sources": ["bis"]})
    assert status == 409 and body["error"].startswith("이미 수집이 실행 중입니다")
    assert api.ecs.run_task.call_count == 0

    # 만료된 락은 무시하고 태스크를 띄운다
    table.put_item(Item={**lock, "expires_at": "2000-01-01T00:00:00+00:00"})
    status, body = _call(api, "POST", "/api/admin/macro/refresh", token=_ADMIN_TOKEN,
                         body={"sources": ["bis"]})
    assert status == 200 and body["task_arn"] == TASK_ARN


def test_admin_review_updates_document(api, table):
    sk = f"{CB_KR_DATE}#{CB_KR_ID}"
    status, body = _call(api, "POST", "/api/admin/macro/review", token=_ADMIN_TOKEN,
                         body={"pk": "DOC#cb_stance#KR", "sk": sk, "action": "approve",
                               "note": "원문 확인"})
    assert status == 200
    assert body["doc"]["review_status"] == "approved"
    assert body["doc"]["reviewed_by"] == ADMIN_EMAIL
    assert body["doc"]["review_note"] == "원문 확인"
    assert body["doc"]["reviewed_at"].startswith("2026-09-20")
    stored = table.get_item(Key={"pk": "DOC#cb_stance#KR", "sk": sk})["Item"]
    assert stored["review_status"] == "approved"

    status, body = _call(api, "POST", "/api/admin/macro/review", token=_ADMIN_TOKEN,
                         body={"pk": "DOC#cb_stance#KR", "sk": sk, "action": "reject"})
    assert status == 200 and body["doc"]["review_status"] == "rejected"


def test_admin_review_validates_target(api):
    status, body = _call(api, "POST", "/api/admin/macro/review", token=_ADMIN_TOKEN,
                         body={"pk": "OBS#policy_rate#KR", "sk": "M#2026-08",
                               "action": "approve"})
    assert status == 400 and "DOC#" in body["error"]

    status, body = _call(api, "POST", "/api/admin/macro/review", token=_ADMIN_TOKEN,
                         body={"pk": "DOC#cb_stance#KR", "sk": "2026-08-22#none",
                               "action": "hold"})
    assert status == 400 and "action" in body["error"]

    status, body = _call(api, "POST", "/api/admin/macro/review", token=_ADMIN_TOKEN,
                         body={"pk": "DOC#cb_stance#KR", "sk": "1999-01-01#zzzz",
                               "action": "approve"})
    assert status == 404 and body["error"] == "해당 문서를 찾을 수 없습니다."


# ---------------- 게이트 / 라우팅 ----------------


def test_macro_requires_authentication(api):
    status, body = _call(api, "GET", "/api/macro/meta", token=None)
    assert status == 401 and body["error"] == "로그인이 필요합니다."


def test_macro_returns_503_when_not_deployed(api):
    api.MACRO_TABLE_NAME = ""
    api._macro_api_instance = None
    status, body = _call(api, "GET", "/api/macro/meta")
    assert status == 503 and body["error"] == "매크로 기능이 배포되지 않았습니다."
    status, body = _call(api, "POST", "/api/admin/macro/refresh", token=_ADMIN_TOKEN,
                         body={"sources": ["bis"]})
    assert status == 503

    # zip에 macro_api.py가 동봉되지 않은 구버전 배포도 같은 응답
    api.MACRO_TABLE_NAME = _ENV["MACRO_TABLE_NAME"]
    api.macro_api = None
    api._macro_api_instance = None
    status, body = _call(api, "GET", "/api/macro/meta")
    assert status == 503


def test_macro_unknown_path_and_method(api):
    status, body = _call(api, "GET", "/api/macro/nope")
    assert status == 404 and body["error"] == "존재하지 않는 API 경로입니다."

    status, body = _call(api, "POST", "/api/macro/meta", body={})
    assert status == 405 and "GET" in body["error"]

    status, body = _call(api, "GET", "/api/admin/macro/refresh", token=_ADMIN_TOKEN)
    assert status == 405 and "POST" in body["error"]


def test_existing_routes_still_work(api):
    """매크로 후크가 기존 라우팅을 가리지 않는지 (회귀 방지)."""
    api.table.scan.return_value = {"Items": []}
    status, body = _call(api, "GET", "/api/runs")
    assert status == 200 and body["runs"] == []


# ---------------- BatchGetItem 경로 (운영에서만 타는 분기) ----------------


def _typed_meta_item(key):
    """BatchGetItem 응답 형태(저수준 AttributeValue)의 SERIES#<id>/META 1건."""
    return {
        "pk": key["pk"],
        "sk": {"S": "META"},
        "id": {"S": key["pk"]["S"].split("#", 1)[1]},
        "name_ko": {"S": "지표"},
        "decimals": {"N": "2"},
        "store_freqs": {"L": [{"S": "M"}]},
        "composite": {"BOOL": False},
    }


def test_batch_get_uses_batch_get_item_and_retries_unprocessed():
    """Table.meta.client가 있으면 BatchGetItem을 쓰고 UnprocessedKeys를 재시도한다."""
    calls = []

    def _batch_get_item(RequestItems):  # noqa: N803 - boto3 시그니처
        calls.append(RequestItems)
        keys = RequestItems["macro-table"]["Keys"]
        done, rest = (keys[:1], keys[1:]) if len(calls) == 1 else (keys, [])
        resp = {"Responses": {"macro-table": [_typed_meta_item(k) for k in done]}}
        if rest:
            resp["UnprocessedKeys"] = {"macro-table": {"Keys": rest}}
        return resp

    fake = MagicMock()
    fake.table_name = "macro-table"
    fake.meta.client.batch_get_item.side_effect = _batch_get_item

    api = macro_api.MacroApi(table=fake, now=lambda: NOW)
    metas = api._indicator_metas(["policy_rate", "cpi_yoy"])
    assert sorted(metas) == ["cpi_yoy", "policy_rate"]
    assert metas["policy_rate"]["decimals"] == 2  # Decimal → int 변환
    assert metas["policy_rate"]["store_freqs"] == ["M"]
    assert len(calls) == 2
    assert calls[0]["macro-table"]["Keys"][0] == {
        "pk": {"S": "SERIES#policy_rate"}, "sk": {"S": "META"},
    }
    assert fake.get_item.call_count == 0


def test_batch_get_falls_back_to_get_item_on_error():
    fake = MagicMock()
    fake.table_name = "macro-table"
    fake.meta.client.batch_get_item.side_effect = RuntimeError("throttled")
    fake.get_item.return_value = {}

    api = macro_api.MacroApi(table=fake, now=lambda: NOW)
    assert api._indicator_metas(["policy_rate"]) == {}
    assert fake.get_item.call_count == 1
