# ============================================================
# [테스트 개요] G20 매크로 저장소 (webui/macro/store.py) + 인메모리 페이크
#
#  - 관측치: 신규 → 동일 값(unchanged) → 값 변경(revisions 누적) → 10개 상한
#  - 시계열 Query: 빈도 필터·기간 범위·부분 기간 상한·limit(최신 N)·페이지네이션
#  - 문서: 저장/최신순 목록/latest_doc/set_review
#  - 파생: rebuild_latest(rank·동순위·prev/change), rebuild_snapshot 구조
#  - 운영: log_ingest(LATEST + next_due 날짜 경계·실패 시 1일 재시도), 수집 락(LOCK#collect),
#          config add_llm_tokens 날짜 리셋
#  - S3: save_raw gzip 복원, s3 없을 때 local_dir 폴백, save_doc_text 키
#  - 페이크 자체: float put → TypeError, ConditionExpression, UpdateExpression 한계,
#    pk별 sk 인덱스(query가 전 항목을 정렬하지 않음 — 결과 동일성 + 성능)
# ============================================================
import gzip
import json
import sys
from pathlib import Path

import pytest

# boto3/botocore는 웹 UI 전용 의존성이라 순수 dev 설치(CI)에는 없을 수 있다.
pytest.importorskip("boto3", reason="webui deps not installed")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "webui"))

from boto3.dynamodb.conditions import Attr, Key  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from macro.fakeddb import FakeS3, FakeTable  # noqa: E402
from macro.schema import Doc, Observation  # noqa: E402
from macro.store import MacroStore  # noqa: E402

NOW = "2026-09-20T06:00:00+00:00"


def _store(table=None, *, s3=None, bucket=None, now=NOW, local_dir=".macro_raw"):
    clock = now if callable(now) else (lambda: now)
    return MacroStore(
        table if table is not None else FakeTable(),
        s3=s3,
        bucket=bucket,
        now=clock,
        local_dir=local_dir,
    )


def _obs(value=3.25, period="2026-08", **kw):
    kw.setdefault("indicator", "policy_rate")
    kw.setdefault("iso", "KR")
    kw.setdefault("freq", "M")
    kw.setdefault("unit", "%")
    kw.setdefault("source", "bis")
    kw.setdefault("series_id", "WS_CBPOL/1.0/D.KR")
    kw.setdefault("source_url", "https://data.bis.org/topics/CBPOL")
    kw.setdefault("method", "월말 기준값 (원천: 일별)")
    kw.setdefault("vintage", "2026-09-01")
    kw.setdefault("retrieved_at", "2026-09-20T06:00:00+00:00")
    return Observation(value=value, period=period, **kw)


def _doc(date="2026-09-18", doc_id="abc123abc123", **kw):
    kw.setdefault("type", "cb_stance")
    kw.setdefault("iso", "KR")
    kw.setdefault("title_ko", "한국은행 금통위 성명")
    kw.setdefault("summary_ko", "기준금리 동점, 완화 기조 시사")
    kw.setdefault("source_url", "https://www.bok.or.kr/")
    kw.setdefault("source_name", "한국은행")
    kw.setdefault("payload", {"stance_score": 0.5, "direction": "hold"})
    return Doc(date=date, id=doc_id, **kw)


# ----------------------------------------------------------------- 관측치


def test_put_observations_new_unchanged_updated():
    table = FakeTable()
    store = _store(table)

    assert store.put_observations([_obs(3.25)]) == {
        "n_new": 1,
        "n_updated": 0,
        "n_unchanged": 0,
        "n_error": 0,
    }
    stored = store.get_observation("policy_rate", "KR", "M", "2026-08")
    assert stored["value"] == 3.25
    assert isinstance(stored["value"], float)  # undecimalize 적용
    assert stored["revisions"] == []
    assert stored["pk"] == "OBS#policy_rate#KR"
    assert stored["sk"] == "M#2026-08"

    # 같은 값 재수집 → 쓰기 없이 unchanged
    writes = table.calls.get("put_item", 0) + table.calls.get("update_item", 0)
    assert store.put_observations([_obs(3.25)])["n_unchanged"] == 1
    assert table.calls.get("update_item", 0) + table.calls.get("put_item", 0) == writes + 1

    # 값이 바뀌면 이전 값이 revisions 맨 앞으로
    res = store.put_observations(
        [_obs(3.5, vintage="2026-09-19", retrieved_at="2026-09-20T06:00:00+00:00")]
    )
    assert res["n_updated"] == 1
    stored = store.get_observation("policy_rate", "KR", "M", "2026-08")
    assert stored["value"] == 3.5
    assert stored["vintage"] == "2026-09-19"
    assert stored["revisions"] == [
        {"value": 3.25, "vintage": "2026-09-01", "retrieved_at": "2026-09-20T06:00:00+00:00"}
    ]


def test_put_observations_touch_unchanged_updates_retrieved_at():
    store = _store()
    store.put_observations([_obs(3.25)])
    store.put_observations(
        [_obs(3.25, retrieved_at="2026-09-21T06:00:00+00:00")], touch_unchanged=True
    )
    assert (
        store.get_observation("policy_rate", "KR", "M", "2026-08")["retrieved_at"]
        == "2026-09-21T06:00:00+00:00"
    )


def test_revisions_capped_at_ten():
    store = _store()
    for i in range(12):
        store.put_observations([_obs(1.25 + i)])
    stored = store.get_observation("policy_rate", "KR", "M", "2026-08")
    assert stored["value"] == 1.25 + 11
    assert len(stored["revisions"]) == 10
    # 최신 개정이 맨 앞 (직전 값 → 그 이전 값 순)
    assert [r["value"] for r in stored["revisions"]] == [1.25 + i for i in range(10, 0, -1)]


def test_payload_observation_revision_keeps_payload():
    store = _store()
    mix = {"items": [{"label": "석탄", "value": 29.1}]}
    store.put_observations([_obs(None, payload=mix, indicator="elec_mix", unit="pct_share")])
    store.put_observations(
        [
            _obs(
                None,
                payload={"items": [{"label": "석탄", "value": 25.0}]},
                indicator="elec_mix",
                unit="pct_share",
            )
        ]
    )
    stored = store.get_observation("elec_mix", "KR", "M", "2026-08")
    assert stored["payload"]["items"][0]["value"] == 25.0
    assert stored["revisions"][0]["payload"] == {"items": [{"label": "석탄", "value": 29.1}]}


def test_put_observations_isolates_errors():
    store = _store()
    res = store.put_observations([object(), _obs(3.25)])
    assert res["n_error"] == 1
    assert res["n_new"] == 1


def test_query_series_range_freq_limit_and_pagination():
    # page_size=2 → LastEvaluatedKey를 따라가는 경로를 강제한다.
    store = _store(FakeTable(page_size=2))
    months = ["2026-05", "2026-06", "2026-07", "2026-08", "2026-09"]
    store.put_observations([_obs(1.25 + i, period=p) for i, p in enumerate(months)])
    store.put_observations([_obs(9.25, freq="Q", period="2026-Q2")])
    store.put_observations([_obs(7.25, iso="US")])

    rows = store.query_series("policy_rate", "KR", "M")
    assert [r["period"] for r in rows] == months  # 시간 오름차순, Q 제외

    rows = store.query_series("policy_rate", "KR", "M", from_period="2026-06", to_period="2026-08")
    assert [r["period"] for r in rows] == ["2026-06", "2026-07", "2026-08"]

    # 부분 기간 상한(연도만) 도 포함 범위로 해석된다
    assert len(store.query_series("policy_rate", "KR", "M", to_period="2026")) == 5

    rows = store.query_series("policy_rate", "KR", "M", limit=2)
    assert [r["period"] for r in rows] == ["2026-08", "2026-09"]  # 최신 2건, 시간순

    assert [r["period"] for r in store.query_series("policy_rate", "KR", "Q")] == ["2026-Q2"]
    assert store.query_series("policy_rate", "JP", "M") == []
    assert store.get_observation("policy_rate", "JP", "M", "2026-08") is None


# ------------------------------------------------------------------- 문서


def test_docs_put_list_latest_and_review():
    store = _store()
    assert (
        store.put_docs(
            [
                _doc(date="2026-08-14", doc_id="aaaaaaaaaaaa"),
                _doc(date="2026-09-18", doc_id="bbbbbbbbbbbb"),
                _doc(
                    type="energy_policy",
                    date="2026-09-01",
                    doc_id="cccccccccccc",
                    payload={"targets": ["2035 NDC"]},
                    ai_generated=True,
                    quotes=["원문 인용"],
                    model_id="haiku",
                    confidence=0.8,
                ),
            ]
        )
        == 3
    )

    docs = store.list_docs("cb_stance", "KR")
    assert [d["sk"] for d in docs] == ["2026-09-18#bbbbbbbbbbbb", "2026-08-14#aaaaaaaaaaaa"]
    assert store.list_docs("cb_stance", "KR", newest_first=False)[0]["date"] == "2026-08-14"
    assert store.list_docs("cb_stance", "KR", limit=1) == docs[:1]

    latest = store.latest_doc("cb_stance", "KR")
    assert latest["id"] == "bbbbbbbbbbbb"
    assert latest["payload"]["stance_score"] == 0.5
    assert store.latest_doc("election", "KR") is None

    ai_doc = store.latest_doc("energy_policy", "KR")
    assert ai_doc["review_status"] == "pending" and ai_doc["confidence"] == 0.8

    updated = store.set_review(ai_doc["pk"], ai_doc["sk"], "approved", "nss", note="확인 완료")
    assert updated["review_status"] == "approved"
    assert updated["reviewed_by"] == "nss"
    assert updated["reviewed_at"] == NOW
    assert updated["review_note"] == "확인 완료"
    with pytest.raises(ValueError, match="review status"):
        store.set_review(ai_doc["pk"], ai_doc["sk"], "maybe", "nss")


# -------------------------------------------------------------- 지표 사전


def test_meta_items_roundtrip():
    store = _store()
    assert (
        store.put_meta_items(
            [
                {"pk": "SERIES#policy_rate", "sk": "META", "name_ko": "정책금리", "decimals": 2},
                {"pk": "SERIES#cpi_yoy", "sk": "META", "name_ko": "소비자물가 상승률"},
                {
                    "pk": "SERIES#__countries__",
                    "sk": "META",
                    "countries": [{"iso": "KR", "name_ko": "한국", "weight": 1.5}],
                },
            ]
        )
        == 3
    )
    assert store.get_meta("policy_rate")["name_ko"] == "정책금리"
    assert store.get_meta("nope") is None
    assert [m["pk"] for m in store.get_meta_many(["cpi_yoy", "nope", "policy_rate"])] == [
        "SERIES#cpi_yoy",
        "SERIES#policy_rate",
    ]
    assert store.get_countries()["countries"][0]["weight"] == 1.5
    assert len(store.list_meta()) == 3
    with pytest.raises(ValueError, match="pk/sk"):
        store.put_meta_items([{"name_ko": "키 없음"}])


# ----------------------------------------------------------- LATEST/SNAPSHOT


def test_rebuild_latest_rank_prev_and_change():
    store = _store()
    store.put_observations(
        [
            _obs(3.25, period="2026-07"),
            _obs(3.75, period="2026-08"),
            _obs(4.5, iso="US"),
            _obs(4.5, iso="GB"),
            _obs(0.5, iso="JP"),
            _obs(99.5, iso="JP", freq="Q", period="2026-Q2"),  # 선호 빈도 M이 먼저
        ]
    )
    rows = store.rebuild_latest("policy_rate", ["KR", "US", "GB", "JP", "CN"])
    by_iso = {r["iso"]: r for r in rows}

    assert set(by_iso) == {"KR", "US", "GB", "JP"}  # 관측치 없는 CN은 생략
    kr = by_iso["KR"]
    assert (kr["pk"], kr["sk"]) == ("LATEST#policy_rate", "KR")
    assert (kr["freq"], kr["period"], kr["value"]) == ("M", "2026-08", 3.75)
    assert (kr["prev_period"], kr["prev_value"]) == ("2026-07", 3.25)
    assert kr["change"] == pytest.approx(0.5)
    assert kr["change_pct"] == pytest.approx(0.5 / 3.25 * 100)
    assert kr["rank"] == 3 and kr["n"] == 4
    assert kr["unit"] == "%" and kr["source"] == "bis"
    assert kr["updated_at"] == NOW

    # 동값은 같은 순위, 그 다음 순위는 건너뛴다 (4.5, 4.5, 3.75, 0.5)
    assert {by_iso["US"]["rank"], by_iso["GB"]["rank"]} == {1}
    assert by_iso["JP"]["rank"] == 4
    assert by_iso["JP"]["period"] == "2026-08"  # Q 관측치가 아니라 M
    assert by_iso["US"]["prev_value"] is None and by_iso["US"]["change"] is None

    assert {r["sk"] for r in store.get_latest("policy_rate")} == {"KR", "US", "GB", "JP"}
    assert [r["sk"] for r in store.get_latest("policy_rate", ["kr"])] == ["KR"]


def test_rebuild_latest_without_rank_for_lcu_units():
    """단위가 국가별로 다른 지표(m2_level lcu_bn·usd_bn 혼재)는 rank=False → rank None, n 0."""
    store = _store()
    store.put_observations([
        _obs(4000.0, indicator="m2_level", iso="KR", unit="lcu_bn"),
        _obs(21.0, indicator="m2_level", iso="US", unit="usd_bn", source="fred"),
        _obs(3000.0, indicator="m2_level", iso="JP", unit="lcu_bn", source="imf"),
    ])
    rows = store.rebuild_latest("m2_level", ["KR", "US", "JP"], rank=False)
    assert {r["iso"] for r in rows} == {"KR", "US", "JP"}
    assert all(r["rank"] is None and r["n"] == 0 for r in rows)
    assert {r["unit"] for r in rows} == {"lcu_bn", "usd_bn"}
    stored = {r["sk"]: r for r in store.get_latest("m2_level")}
    assert stored["KR"]["rank"] is None and stored["KR"]["value"] == 4000.0
    # 기본(rank=True)은 기존 동작
    ranked = {r["iso"]: r for r in store.rebuild_latest("m2_level", ["KR", "US", "JP"])}
    assert ranked["KR"]["rank"] == 1 and ranked["US"]["rank"] == 3 and ranked["KR"]["n"] == 3


def test_rebuild_snapshot_structure():
    store = _store()
    store.put_observations([_obs(3.75), _obs(2.25, indicator="cpi_yoy", iso="KR")])
    store.rebuild_latest("policy_rate", ["KR"])
    store.rebuild_latest("cpi_yoy", ["KR"])
    store.put_docs([_doc(), _doc(date="2026-09-19", doc_id="dddddddddddd")])

    snap = store.rebuild_snapshot("kr", ["policy_rate", "cpi_yoy", "gdp_usd"])
    assert (snap["pk"], snap["sk"], snap["iso"]) == ("SNAPSHOT#KR", "PROFILE", "KR")
    assert snap["updated_at"] == NOW
    assert set(snap["latest"]) == {"policy_rate", "cpi_yoy"}  # LATEST 없는 지표는 생략
    assert snap["latest"]["policy_rate"]["value"] == 3.75
    assert "pk" not in snap["latest"]["policy_rate"]
    assert set(snap["docs"]) == {"cb_stance"}
    doc = snap["docs"]["cb_stance"]
    assert doc["id"] == "dddddddddddd"
    assert doc["payload"]["stance_score"] == 0.5
    assert doc["ai_generated"] is False and doc["review_status"] == "approved"
    assert doc["source_url"] and doc["date"] == "2026-09-19" and doc["quotes"] == []
    assert store.get_snapshot("KR")["latest"]["cpi_yoy"]["value"] == 2.25
    assert store.get_snapshot("JP") is None


# ---------------------------------------------------------------- INGEST


def test_log_ingest_latest_and_next_due():
    store = _store()
    store.log_ingest(
        "bis",
        {
            "status": "ok",
            "started_at": "2026-09-19T05:00:00+00:00",
            "finished_at": "2026-09-19T05:02:00+00:00",
            "n_obs": 10,
        },
    )
    latest = store.log_ingest(
        "bis",
        {
            "status": "partial",
            "started_at": "2026-09-20T05:00:00+00:00",
            "finished_at": "2026-09-20T05:03:00+00:00",
            "n_obs": 12,
            "n_docs": 0,
            "errors": ["TR 실패"],
            "countries_ok": ["KR", "US"],
            "countries_failed": ["TR"],
            "cadence_days": 7,
        },
    )
    assert latest["sk"] == "LATEST"
    # partial은 케이던스(7일)와 무관하게 다음 날 00:00 UTC 재시도. cadence_days는 그대로.
    assert latest["next_due"] == "2026-09-21T00:00:00+00:00"
    assert latest["cadence_days"] == 7 and "partial" in latest["retry_reason"]
    assert latest["status"] == "partial" and latest["n_obs"] == 12

    assert store.get_ingest_latest("bis") == latest
    assert store.get_ingest_latest("oecd") is None

    runs = store.list_ingest("bis")
    assert [r["sk"] for r in runs] == ["2026-09-20T05:03:00+00:00", "2026-09-19T05:02:00+00:00"]
    assert "next_due" not in runs[0]
    assert runs[0]["run_ts"] == "2026-09-20T05:03:00+00:00"
    assert len(store.list_ingest("bis", limit=1)) == 1

    # finished_at이 없으면 주입된 now를 실행 시각으로 쓴다
    assert store.log_ingest("yahoo", {"n_obs": 3})["finished_at"] == NOW


def test_next_due_uses_date_boundary_and_retry_on_failure():
    """next_due = (finished_at 날짜 + cadence_days) 00:00 UTC — 매일 21:00 UTC 고정 기동에서
    daily 소스가 격일로 밀리지 않는다. failed/partial은 1일 뒤 00:00 UTC."""
    store = _store()
    ok = store.log_ingest("bis", {"status": "ok", "finished_at": "2026-09-19T21:03:12+00:00",
                                  "cadence_days": 1})
    assert ok["next_due"] == "2026-09-20T00:00:00+00:00"  # day2 21:00 기동 시 now >= next_due
    assert "retry_reason" not in ok

    weekly = store.log_ingest("oecd", {"status": "ok", "finished_at": "2026-09-19T21:03:12+00:00",
                                       "cadence_days": 7})
    assert weekly["next_due"] == "2026-09-26T00:00:00+00:00"
    monthly = store.log_ingest("ember", {"status": "ok", "finished_at": "2026-09-19T21:03:12+00:00",
                                         "cadence_days": 30})
    assert monthly["next_due"] == "2026-10-19T00:00:00+00:00"
    quarterly = store.log_ingest("wits", {"status": "ok", "finished_at": "2026-09-19T21:03:12+00:00",
                                          "cadence_days": 90})
    assert quarterly["next_due"] == "2026-12-18T00:00:00+00:00"

    # KST 같은 다른 시간대 표기는 UTC 날짜로 환산해 경계를 잡는다 (06:03 KST = 전날 21:03 UTC)
    kst = store.log_ingest("yahoo", {"status": "ok", "finished_at": "2026-09-20T06:03:12+09:00",
                                     "cadence_days": 1})
    assert kst["next_due"] == "2026-09-20T00:00:00+00:00"

    failed = store.log_ingest("imf", {"status": "failed", "finished_at": "2026-09-19T21:03:12+00:00",
                                      "cadence_days": 30})
    assert failed["next_due"] == "2026-09-20T00:00:00+00:00"
    assert failed["cadence_days"] == 30 and failed["retry_reason"].startswith("status=failed")
    # 다음 실행이 성공하면 retry_reason 없이 정상 케이던스로 돌아간다
    back = store.log_ingest("imf", {"status": "ok", "finished_at": "2026-09-20T21:03:12+00:00",
                                    "cadence_days": 30})
    assert back["next_due"] == "2026-10-20T00:00:00+00:00" and "retry_reason" not in back


def test_collect_lock_acquire_release_and_ttl_takeover():
    """CONFIG / LOCK#collect: 조건부 획득, holder 일치 해제, 만료 후 넘겨받기."""
    table = FakeTable()
    a = _store(table, now="2026-09-20T06:00:00+00:00")
    assert a.acquire_lock("run-a") is True
    lock = a.get_lock()
    assert (lock["pk"], lock["sk"]) == ("CONFIG", "LOCK#collect")
    assert lock["holder"] == "run-a" and lock["expires_at"] == "2026-09-20T09:00:00+00:00"
    assert a.lock_active()["holder"] == "run-a"

    b = _store(table, now="2026-09-20T06:30:00+00:00")
    assert b.acquire_lock("run-b") is False          # 만료 전 → 실패
    assert b.release_lock("run-b") is False          # 남의 락은 지우지 못한다
    assert b.get_lock()["holder"] == "run-a"

    assert a.release_lock("run-a") is True
    assert a.get_lock() is None and a.lock_active() is None
    assert a.release_lock("run-a") is False          # 이미 없음

    a.acquire_lock("run-a")
    late = _store(table, now="2026-09-20T09:00:01+00:00")
    assert late.lock_active() is None                # 만료된 락은 활성 아님
    assert late.acquire_lock("run-c") is True        # TTL 지난 락은 넘겨받는다
    assert late.get_lock()["holder"] == "run-c"
    assert late.acquire_lock("run-d", ttl_seconds=60) is False


# ---------------------------------------------------------------- CONFIG


def test_config_and_llm_token_budget_daily_reset():
    clock = {"t": "2026-09-20T06:00:00+00:00"}
    store = _store(now=lambda: clock["t"])

    cfg = store.get_config()
    assert cfg == {
        "llm_daily_token_budget": 2_000_000,
        "llm_tokens_used_today": 0,
        "llm_tokens_date": "2026-09-20",
    }

    assert store.add_llm_tokens(100) == 100
    assert store.add_llm_tokens(50) == 150
    cfg = store.get_config()
    assert cfg["llm_tokens_used_today"] == 150
    assert cfg["llm_tokens_date"] == "2026-09-20"

    clock["t"] = "2026-09-21T00:30:00+00:00"
    assert store.get_config()["llm_tokens_used_today"] == 0  # 날짜가 지나면 0으로 정규화
    assert store.add_llm_tokens(30) == 30
    assert store.add_llm_tokens(5) == 35
    assert store.get_config()["llm_tokens_date"] == "2026-09-21"

    assert store.set_config(llm_daily_token_budget=1_000_000)["llm_daily_token_budget"] == 1_000_000
    assert store.get_config()["llm_tokens_used_today"] == 35


# -------------------------------------------------------------------- S3


def test_save_raw_gzip_roundtrip_and_doc_text():
    s3 = FakeS3()
    store = _store(s3=s3, bucket="data-bucket")

    key = store.save_raw("bis", "policy_rate KR", {"obs": [1, 2], "한글": "값"})
    assert key == "macro/raw/bis/2026-09-20/policy_rate_KR.json.gz"
    rec = s3.objects[key]
    assert rec["ContentEncoding"] == "gzip" and rec["ContentType"] == "application/json"
    assert json.loads(gzip.decompress(rec["Body"]).decode()) == {"obs": [1, 2], "한글": "값"}

    # 이미 gzip인 bytes는 다시 압축하지 않는다
    raw = gzip.compress(b'{"a":1}')
    key2 = store.save_raw("oecd", "cpi", raw)
    assert gzip.decompress(s3.objects[key2]["Body"]) == b'{"a":1}'
    assert gzip.decompress(s3.objects[store.save_raw("imf", "m2", "plain")]["Body"]) == b"plain"

    doc = _doc()
    doc_key = store.save_doc_text(doc, "# 성명 전문\n인용...")
    assert doc_key == "macro/docs/cb_stance/KR/2026-09-18_abc123abc123.md"
    assert doc.s3_key == doc_key  # 이어서 put_docs([doc])하면 전문 링크가 붙는다
    assert s3.objects[doc_key]["Body"].decode().startswith("# 성명 전문")
    assert store.put_docs([doc]) == 1
    assert store.latest_doc("cb_stance", "KR")["s3_key"] == doc_key


def test_save_raw_local_fallback_and_failure(tmp_path):
    store = _store(local_dir=tmp_path)  # s3=None → dry-run 로컬 저장
    key = store.save_raw("ember", "elec_mix", {"a": 1})
    assert key == "macro/raw/ember/2026-09-20/elec_mix.json.gz"
    assert json.loads(gzip.decompress((tmp_path / key).read_bytes())) == {"a": 1}

    # 직렬화 불가한 입력도 예외 대신 None (수집을 죽이지 않는다)
    assert store.save_raw("ember", "bad", 12345) is None


# ------------------------------------------------------- 페이크 자체 검증


def test_fake_table_rejects_float_like_dynamodb():
    table = FakeTable()
    with pytest.raises(TypeError, match="Float types are not supported"):
        table.put_item(Item={"pk": "OBS#x#KR", "sk": "M#2026-08", "value": 3.5})
    with pytest.raises(TypeError, match="Float types are not supported"):
        table.put_item(Item={"pk": "a", "sk": "b", "payload": {"items": [{"v": 1.0}]}})
    with pytest.raises(TypeError, match="Float types are not supported"):
        table.update_item(
            Key={"pk": "a", "sk": "b"},
            UpdateExpression="SET v = :v",
            ExpressionAttributeValues={":v": 1.5},
        )
    assert table.items == {}


def test_fake_table_condition_expression_forms():
    table = FakeTable()
    item = {"pk": "CONFIG", "sk": "MACRO", "n": 1}
    table.put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
    for cond in ("attribute_not_exists(pk)", Attr("pk").not_exists()):
        with pytest.raises(ClientError) as ei:
            table.put_item(Item=item, ConditionExpression=cond)
        assert ei.value.response["Error"]["Code"] == "ConditionalCheckFailedException"
    # 조건이 참이면 통과
    table.put_item(Item=item, ConditionExpression=Attr("n").eq(1))
    table.update_item(
        Key={"pk": "CONFIG", "sk": "MACRO"},
        UpdateExpression="SET s = :s",
        ConditionExpression=Attr("missing").not_exists() | Attr("n").ne(1),
        ExpressionAttributeValues={":s": "ok"},
    )
    with pytest.raises(ClientError):
        table.update_item(
            Key={"pk": "CONFIG", "sk": "MACRO"},
            UpdateExpression="SET s = :s",
            ConditionExpression="#s = :want",
            ExpressionAttributeNames={"#s": "s"},
            ExpressionAttributeValues={":s": "no", ":want": "other"},
        )


def test_fake_table_update_expression_support():
    from decimal import Decimal

    table = FakeTable()
    key = {"pk": "OBS#x#KR", "sk": "M#2026-08"}
    table.put_item(Item={**key, "value": Decimal("1.5")})
    resp = table.update_item(
        Key=key,
        UpdateExpression=(
            "SET #v = :v, revisions = list_append(if_not_exists(revisions, :empty), :r) ADD hits :one"
        ),
        ExpressionAttributeNames={"#v": "value"},
        ExpressionAttributeValues={
            ":v": Decimal("2.5"),
            ":empty": [],
            ":r": [{"value": Decimal("1.5")}],
            ":one": Decimal(1),
        },
        ReturnValues="ALL_NEW",
    )
    item = resp["Attributes"]
    assert item["value"] == Decimal("2.5")
    assert item["revisions"] == [{"value": Decimal("1.5")}]
    assert item["hits"] == Decimal(1)

    table.update_item(Key=key, UpdateExpression="REMOVE hits, revisions")
    assert set(table.get_item(Key=key)["Item"]) == {"pk", "sk", "value"}

    # update_item은 없는 항목을 만들고, ADD는 0에서 시작한다
    table.update_item(
        Key={"pk": "CONFIG", "sk": "MACRO"},
        UpdateExpression="ADD used :n",
        ExpressionAttributeValues={":n": Decimal(7)},
    )
    assert table.get_item(Key={"pk": "CONFIG", "sk": "MACRO"})["Item"]["used"] == Decimal(7)

    for bad in ("DELETE tags :t", "INCREMENT n :n", "SET a[0] = :n"):
        with pytest.raises(NotImplementedError):
            table.update_item(
                Key=key, UpdateExpression=bad, ExpressionAttributeValues={":n": Decimal(1), ":t": 1}
            )


def test_fake_table_query_and_batch_writer():
    table = FakeTable(page_size=2)
    for i, sk in enumerate(["M#2026-06", "M#2026-07", "M#2026-08", "Q#2026-Q2"]):
        table.put_item(Item={"pk": "OBS#x#KR", "sk": sk, "n": i})
    resp = table.query(KeyConditionExpression=Key("pk").eq("OBS#x#KR") & Key("sk").eq("M#2026-07"))
    assert resp["Count"] == 1 and "LastEvaluatedKey" not in resp

    resp = table.query(
        KeyConditionExpression=Key("pk").eq("OBS#x#KR") & Key("sk").begins_with("M#"),
        ScanIndexForward=False,
        Limit=2,
    )
    assert [i["sk"] for i in resp["Items"]] == ["M#2026-08", "M#2026-07"]
    assert resp["LastEvaluatedKey"] == {"pk": "OBS#x#KR", "sk": "M#2026-07"}

    resp = table.query(
        KeyConditionExpression=Key("pk").eq("OBS#x#KR")
        & Key("sk").between("M#2026-07", "M#2026-08￿"),
        ExclusiveStartKey={"pk": "OBS#x#KR", "sk": "M#2026-07"},
    )
    assert [i["sk"] for i in resp["Items"]] == ["M#2026-08"]

    # pk 동등 조건 없는 Query는 DynamoDB처럼 거부
    with pytest.raises(ClientError):
        table.query(KeyConditionExpression=Key("sk").begins_with("M#"))

    with table.batch_writer() as bw:
        bw.put_item(Item={"pk": "OBS#x#JP", "sk": "M#2026-08"})
        bw.delete_item(Key={"pk": "OBS#x#KR", "sk": "Q#2026-Q2"})
        assert len(table.items) == 4  # 아직 flush 전
    assert len(table.items) == 4 and ("OBS#x#JP", "M#2026-08") in table.items
    assert table.scan(Limit=1)["Count"] == 1
    assert len(table.scan()["Items"]) == 2  # page_size=2


def test_fake_table_query_uses_partition_index():
    """query/scan 결과는 그대로이고(정렬·페이지네이션 포함) 전 항목 정렬은 하지 않는다.

    pk별 sk 인덱스 도입 전에는 query마다 전 항목을 정렬해 55k 항목·4.5k 쿼리에 221초가
    걸렸다. 기준: 10k 항목(100 pk × 100 sk) · 100 쿼리 < 1초.
    """
    import time
    from decimal import Decimal

    table = FakeTable(page_size=10_000)
    expected = {}
    for i in range(100):
        pk = f"OBS#ind{i}#KR"
        sks = [f"M#{2000 + m // 12:04d}-{m % 12 + 1:02d}" for m in range(100)]
        for m, sk in enumerate(sks):
            table.put_item(Item={"pk": pk, "sk": sk, "n": Decimal(m)})
        expected[pk] = sorted(sks)
    assert len(table.items) == 10_000

    t0 = time.perf_counter()
    for pk, sks in expected.items():
        resp = table.query(KeyConditionExpression=Key("pk").eq(pk) & Key("sk").begins_with("M#"))
        assert [it["sk"] for it in resp["Items"]] == sks
    elapsed = time.perf_counter() - t0
    assert elapsed < 1.0, f"query 100회가 {elapsed:.2f}초 (인덱스 회귀?)"

    # 결과 동일성: 역순·범위·삭제 후 상태 · scan은 (pk, sk) 오름차순 전체
    back = table.query(
        KeyConditionExpression=Key("pk").eq("OBS#ind0#KR") & Key("sk").begins_with("M#"),
        ScanIndexForward=False,
        Limit=3,
    )
    assert [it["sk"] for it in back["Items"]] == expected["OBS#ind0#KR"][-1:-4:-1]
    table.delete_item(Key={"pk": "OBS#ind0#KR", "sk": expected["OBS#ind0#KR"][0]})
    left = table.query(KeyConditionExpression=Key("pk").eq("OBS#ind0#KR"))
    assert [it["sk"] for it in left["Items"]] == expected["OBS#ind0#KR"][1:]
    scanned = table.scan(Limit=10_000)["Items"]
    assert len(scanned) == 9_999
    assert [(it["pk"], it["sk"]) for it in scanned] == sorted(
        (it["pk"], it["sk"]) for it in scanned
    )


def test_fake_s3_missing_key_and_listing():
    s3 = FakeS3({"macro/raw/a.json.gz": b"x"})
    assert s3.get_object(Bucket="b", Key="macro/raw/a.json.gz")["Body"].read() == b"x"
    assert s3.head_object(Bucket="b", Key="macro/raw/a.json.gz")["ContentLength"] == 1
    assert [o["Key"] for o in s3.list_objects_v2(Bucket="b", Prefix="macro/")["Contents"]] == [
        "macro/raw/a.json.gz"
    ]
    assert "Contents" not in s3.list_objects_v2(Bucket="b", Prefix="none/")
    s3.delete_object(Bucket="b", Key="macro/raw/a.json.gz")
    for call in (s3.get_object, s3.head_object):
        with pytest.raises(ClientError) as ei:
            call(Bucket="b", Key="macro/raw/a.json.gz")
        assert ei.value.response["Error"]["Code"] == "NoSuchKey"
