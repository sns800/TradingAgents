# ============================================================
# [모듈 개요] G20 매크로 저장소 — DynamoDB 단일 테이블 + S3 원본/문서 보존
#
# CONTRACT.md 4장(키 설계)·5장(문서)·8장(S3 레이아웃)을 코드로 고정한 유일한 지점.
# 수집기(sources/*, llm/*), 파생/집계, Lambda API가 모두 이 클래스를 통해 읽고 쓴다.
# 키 문자열을 다른 모듈에 하드코딩하지 말고 여기의 메서드를 쓸 것.
#
# 키 요약 (pk / sk)
#   SERIES#<indicator>      / META                  지표 사전 (countries는 SERIES#__countries__)
#   OBS#<indicator>#<iso>   / <freq>#<period>       관측치 + revisions(최대 10)
#   LATEST#<indicator>      / <iso>                 개요 히트맵용 최신값 + rank
#   SNAPSHOT#<iso>          / PROFILE               국가 프로필 (latest + docs)
#   DOC#<type>#<iso>        / <date>#<id>           정성 문서
#   INGEST#<source>         / <run_ts> | LATEST     수집 로그 + next_due
#   CONFIG                  / MACRO                 LLM 토큰 예산
#   CONFIG                  / LOCK#collect          수집기 동시 실행 방지 락 (expires_at·holder)
#
# 규칙
#  - 쓰기: Observation.to_item()/Doc.to_item() 또는 schema._decimalize를 통과시켜
#    float가 DynamoDB에 닿지 않게 한다 (put_item은 float를 거부한다).
#  - 읽기: 모든 반환값에 undecimalize를 적용해 Decimal 없는 JSON 친화 dict로 돌려준다.
#  - 항목 단위 실패 격리: 관측치/문서 저장은 한 건 실패가 배치 전체를 죽이지 않는다.
#  - 시각은 생성자의 now 콜러블로 주입 가능 (테스트 결정성).
#  - table 인자는 boto3 Table 또는 macro.fakeddb.FakeTable (같은 호출 규약).
#
# 사용 예:
#   store = MacroStore(boto3.resource("dynamodb").Table(os.environ["MACRO_TABLE_NAME"]),
#                      s3=boto3.client("s3"), bucket=os.environ["DATA_BUCKET"])
#   store.put_observations(obs)          # {'n_new': 12, 'n_updated': 1, ...}
#   store.rebuild_latest("policy_rate", ["KR", "US"])
# ============================================================
from __future__ import annotations

import gzip
import json
import logging
import re
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from macro.schema import Doc, Observation, _decimalize, now_iso, undecimalize

logger = logging.getLogger("macro.store")

MAX_REVISIONS = 10
DEFAULT_FREQ_PREF: tuple[str, ...] = ("M", "Q", "Y", "D", "W", "E")
DEFAULT_DOC_TYPES: tuple[str, ...] = (
    "cb_stance",
    "energy_policy",
    "election",
    "poll_of_polls",
    "not_applicable",
)
DEFAULT_LLM_DAILY_TOKEN_BUDGET = 2_000_000
REVIEW_STATUSES = ("pending", "approved", "rejected")
COUNTRIES_META_ID = "__countries__"
CONFIG_KEY = {"pk": "CONFIG", "sk": "MACRO"}
LOCK_KEY = {"pk": "CONFIG", "sk": "LOCK#collect"}
# 락 TTL: 한 번의 전체 수집이 이보다 오래 걸리면 다른 기동이 락을 빼앗아도 된다고 본다
LOCK_TTL_SECONDS = 3 * 3600
# 실패·부분 실패 run은 케이던스와 무관하게 다음 날 재시도한다 (CONTRACT 12장)
RETRY_STATUSES = ("failed", "partial")
RETRY_DAYS = 1
# sk 범위 질의의 상한 센티넬: "M#2026-08￿" 는 "M#2026-08"을 포함하고
# "M#2026-09" 보다 작으므로 완전/부분 기간 상한을 한 가지 규칙으로 처리할 수 있다.
_SK_MAX = "￿"
# LATEST 문서 요약에 담는 필드 (SNAPSHOT이 커지지 않게 전문(s3_key)은 참조만 둔다)
_DOC_SUMMARY_FIELDS = (
    "pk",
    "sk",
    "type",
    "iso",
    "date",
    "id",
    "title_ko",
    "summary_ko",
    "source_url",
    "source_name",
    "payload",
    "ai_generated",
    "model_id",
    "confidence",
    "quotes",
    "review_status",
    "reviewed_by",
    "reviewed_at",
    "s3_key",
    "retrieved_at",
)
_SAFE_NAME_RE = re.compile(r"[^0-9A-Za-z._-]+")


def _code(err: ClientError) -> str:
    return str(err.response.get("Error", {}).get("Code", ""))


def _is_conditional_failure(err: ClientError) -> bool:
    return _code(err) == "ConditionalCheckFailedException"


class MacroStore:
    """매크로 데이터의 DynamoDB/S3 접근 계층 (CONTRACT 4·5·8장)."""

    def __init__(
        self,
        table: Any,
        *,
        s3: Any = None,
        bucket: str | None = None,
        now: Callable[[], Any] | None = None,
        local_dir: str | Path = ".macro_raw",
    ):
        self.table = table
        self.s3 = s3
        self.bucket = bucket
        self.local_dir = Path(local_dir)
        self._now_fn: Callable[[], Any] = now or now_iso

    # ------------------------------------------------------------ 시각/공통

    def _now_iso(self) -> str:
        v = self._now_fn()
        if isinstance(v, datetime):
            if v.tzinfo is None:
                v = v.replace(tzinfo=timezone.utc)
            return v.astimezone(timezone.utc).isoformat(timespec="seconds")
        return str(v)

    def _today(self) -> str:
        return self._now_iso()[:10]

    def _query_all(
        self,
        key_cond: Any,
        *,
        forward: bool = True,
        limit: int | None = None,
    ) -> list[dict]:
        """LastEvaluatedKey를 따라가며 모든 페이지를 모은다 (limit 건까지)."""
        out: list[dict] = []
        start: dict | None = None
        while True:
            kw: dict[str, Any] = {"KeyConditionExpression": key_cond, "ScanIndexForward": forward}
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

    def _put(self, item: dict) -> dict:
        """updated_at을 붙이고 float를 Decimal로 바꿔 저장한다."""
        item = _decimalize(dict(item))
        item.setdefault("updated_at", self._now_iso())
        self.table.put_item(Item=item)
        return item

    # ----------------------------------------------------- 관측치 (OBS#)

    def put_observations(
        self, obs: Iterable[Observation], *, touch_unchanged: bool = False
    ) -> dict[str, int]:
        """관측치를 저장한다 (신규 삽입 / 값 변경 시 revisions 누적).

        반환: {"n_new", "n_updated", "n_unchanged", "n_error"} — 수집 로그 요약에 그대로 쓴다.
        touch_unchanged=True면 값이 같아도 retrieved_at을 갱신한다(쓰기 1회 추가).
        """
        stats = {"n_new": 0, "n_updated": 0, "n_unchanged": 0, "n_error": 0}
        for o in obs:
            try:
                self._put_observation(o, stats, touch_unchanged=touch_unchanged)
            except Exception as e:  # noqa: BLE001 - 항목 단위 실패 격리
                stats["n_error"] += 1
                logger.warning(
                    "관측치 저장 실패 %s/%s %s %s: %s",
                    getattr(o, "indicator", "?"),
                    getattr(o, "iso", "?"),
                    getattr(o, "freq", "?"),
                    getattr(o, "period", "?"),
                    e,
                )
        return stats

    def _put_observation(
        self, o: Observation, stats: dict[str, int], *, touch_unchanged: bool
    ) -> None:
        item = o.to_item()
        now = self._now_iso()
        item["updated_at"] = now
        item["revisions"] = []
        try:
            self.table.put_item(Item=item, ConditionExpression=Attr("pk").not_exists())
            stats["n_new"] += 1
            return
        except ClientError as e:
            if not _is_conditional_failure(e):
                raise

        key = {"pk": item["pk"], "sk": item["sk"]}
        old = self.table.get_item(Key=key).get("Item") or {}
        if _same_observed_value(old, item):
            if touch_unchanged:
                self.table.update_item(
                    Key=key,
                    UpdateExpression="SET retrieved_at = :r, updated_at = :u",
                    ExpressionAttributeValues={":r": item["retrieved_at"], ":u": now},
                )
            stats["n_unchanged"] += 1
            return

        revision = {
            "value": old.get("value"),
            "vintage": old.get("vintage"),
            "retrieved_at": old.get("retrieved_at"),
        }
        if old.get("value") is None and old.get("payload") is not None:
            # 복합값(elec_mix 등)은 value가 없으므로 이전 payload를 함께 남긴다.
            revision["payload"] = old["payload"]
        revisions = [revision, *(old.get("revisions") or [])][:MAX_REVISIONS]

        sets: dict[str, Any] = {
            "value": item.get("value"),
            "vintage": item["vintage"],
            "retrieved_at": item["retrieved_at"],
            "method": item["method"],
            "flags": item["flags"],
            "source": item["source"],
            "series_id": item["series_id"],
            "source_url": item["source_url"],
            "unit": item["unit"],
            "revisions": revisions,
            "updated_at": now,
        }
        removes: list[str] = []
        if item.get("payload") is not None:
            sets["payload"] = item["payload"]
        elif old.get("payload") is not None:
            removes.append("payload")

        names = {f"#k{i}": k for i, k in enumerate(sets)}
        values = {f":v{i}": v for i, v in enumerate(sets.values())}
        expr = "SET " + ", ".join(f"#k{i} = :v{i}" for i in range(len(sets)))
        for i, attr in enumerate(removes):
            names[f"#r{i}"] = attr
        if removes:
            expr += " REMOVE " + ", ".join(f"#r{i}" for i in range(len(removes)))
        self.table.update_item(
            Key=key,
            UpdateExpression=expr,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        stats["n_updated"] += 1

    def get_observation(self, indicator: str, iso: str, freq: str, period: str) -> dict | None:
        item = self.table.get_item(
            Key={"pk": obs_pk(indicator, iso), "sk": f"{freq}#{period}"}
        ).get("Item")
        return undecimalize(item) if item else None

    def query_series(
        self,
        indicator: str,
        iso: str,
        freq: str,
        from_period: str | None = None,
        to_period: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """한 국가·빈도의 시계열을 **시간 오름차순**으로 돌려준다.

        limit을 주면 최신 limit건만 (내림차순 조회 후 되돌려 정렬) 반환한다.
        from/to는 부분 기간(예: freq=M에 to="2026")도 허용한다.
        """
        pk = obs_pk(indicator, iso)
        if from_period is None and to_period is None:
            cond = Key("pk").eq(pk) & Key("sk").begins_with(f"{freq}#")
        else:
            lo = f"{freq}#{from_period}" if from_period else f"{freq}#"
            hi = f"{freq}#{to_period}{_SK_MAX}" if to_period else f"{freq}#{_SK_MAX}"
            cond = Key("pk").eq(pk) & Key("sk").between(lo, hi)
        forward = limit is None
        rows = self._query_all(cond, forward=forward, limit=limit)
        if not forward:
            rows.reverse()
        return [undecimalize(r) for r in rows]

    # ----------------------------------------------------------- 문서 (DOC#)

    def put_docs(self, docs: Iterable[Doc]) -> int:
        """문서를 저장한다(같은 date#id면 덮어쓰기). 반환: 저장 성공 건수."""
        n = 0
        for d in docs:
            try:
                item = d.to_item()
                item["updated_at"] = self._now_iso()
                self.table.put_item(Item=item)
                n += 1
            except Exception as e:  # noqa: BLE001 - 문서 단위 실패 격리
                logger.warning(
                    "문서 저장 실패 %s/%s %s: %s",
                    getattr(d, "type", "?"),
                    getattr(d, "iso", "?"),
                    getattr(d, "date", "?"),
                    e,
                )
        return n

    def list_docs(
        self, type: str, iso: str, limit: int = 12, newest_first: bool = True  # noqa: A002
    ) -> list[dict]:
        cond = Key("pk").eq(doc_pk(type, iso))
        rows = self._query_all(cond, forward=not newest_first, limit=limit)
        return [undecimalize(r) for r in rows]

    def latest_doc(self, type: str, iso: str) -> dict | None:  # noqa: A002
        rows = self.list_docs(type, iso, limit=1, newest_first=True)
        return rows[0] if rows else None

    def set_review(
        self, pk: str, sk: str, status: str, reviewed_by: str, note: str | None = None
    ) -> dict | None:
        """문서 검토 상태를 바꾼다 (POST /api/admin/macro/review)."""
        if status not in REVIEW_STATUSES:
            raise ValueError(f"unknown review status {status!r}")
        sets: dict[str, Any] = {
            "review_status": status,
            "reviewed_by": reviewed_by,
            "reviewed_at": self._now_iso(),
            "updated_at": self._now_iso(),
        }
        if note:
            sets["review_note"] = note
        names = {f"#k{i}": k for i, k in enumerate(sets)}
        values = {f":v{i}": v for i, v in enumerate(sets.values())}
        resp = self.table.update_item(
            Key={"pk": pk, "sk": sk},
            UpdateExpression="SET " + ", ".join(f"#k{i} = :v{i}" for i in range(len(sets))),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )
        item = resp.get("Attributes")
        return undecimalize(item) if item else None

    # ------------------------------------------------------ 지표 사전 (SERIES#)

    def put_meta_items(self, items: list[dict]) -> int:
        """registry가 만든 SERIES#.../META 항목들을 그대로 저장한다."""
        n = 0
        for it in items:
            if "pk" not in it or "sk" not in it:
                raise ValueError("meta item에 pk/sk가 필요함")
            self._put(it)
            n += 1
        return n

    def get_meta(self, indicator: str) -> dict | None:
        item = self.table.get_item(Key={"pk": f"SERIES#{indicator}", "sk": "META"}).get("Item")
        return undecimalize(item) if item else None

    def get_meta_many(self, ids: Iterable[str]) -> list[dict]:
        """지표 id 목록으로 META를 get_item 반복 조회한다 (SERIES#는 pk가 여러 개)."""
        out = []
        for i in ids:
            item = self.get_meta(i)
            if item:
                out.append(item)
        return out

    def get_countries(self) -> dict | None:
        """국가 목록 메타 (`SERIES#__countries__` / META)."""
        return self.get_meta(COUNTRIES_META_ID)

    def list_meta(self) -> list[dict]:
        """모든 SERIES# META (scan 기반 — devserver/점검용. 운영은 get_meta_many 사용)."""
        out: list[dict] = []
        start: dict | None = None
        while True:
            kw: dict[str, Any] = {}
            if start:
                kw["ExclusiveStartKey"] = start
            resp = self.table.scan(**kw)
            for it in resp.get("Items", []):
                if str(it.get("pk", "")).startswith("SERIES#") and it.get("sk") == "META":
                    out.append(undecimalize(it))
            start = resp.get("LastEvaluatedKey")
            if not start:
                break
        return sorted(out, key=lambda x: str(x.get("pk")))

    # ------------------------------------------------- 파생 항목 (LATEST/SNAPSHOT)

    def rebuild_latest(
        self,
        indicator: str,
        isos: Sequence[str],
        freq_pref: Sequence[str] = DEFAULT_FREQ_PREF,
        *,
        rank: bool = True,
    ) -> list[dict]:
        """국가별 최신 관측을 모아 `LATEST#<indicator>` 항목을 다시 만든다.

        선호 빈도 순서대로 sk 내림차순 Limit 2 Query를 던져 최신값과 직전값을 얻고,
        value 내림차순 rank(동값은 같은 순위)와 n(랭킹 대상 수)을 계산해 저장한다.
        `rank=False`면 국가 간 절대값 비교가 무의미한 지표(단위가 국가별로 다른 `m2_level`
        `lcu_bn` 등)로 보고 rank를 None, n을 0으로 둔다.
        """
        now = self._now_iso()
        records: list[dict] = []
        for iso in isos:
            iso = iso.upper()
            latest = prev = None
            for freq in freq_pref:
                resp = self.table.query(
                    KeyConditionExpression=Key("pk").eq(obs_pk(indicator, iso))
                    & Key("sk").begins_with(f"{freq}#"),
                    ScanIndexForward=False,
                    Limit=2,
                )
                items = resp.get("Items", [])
                if items:
                    latest = undecimalize(items[0])
                    prev = undecimalize(items[1]) if len(items) > 1 else None
                    break
            if latest is None:
                logger.debug("LATEST 건너뜀 (관측치 없음): %s/%s", indicator, iso)
                continue
            value = latest.get("value")
            prev_value = prev.get("value") if prev else None
            change = change_pct = None
            if value is not None and prev_value is not None:
                change = value - prev_value
                if prev_value != 0:
                    change_pct = change / abs(prev_value) * 100
            rec: dict[str, Any] = {
                "pk": latest_pk(indicator),
                "sk": iso,
                "indicator": indicator,
                "iso": iso,
                "freq": latest.get("freq"),
                "period": latest.get("period"),
                "value": value,
                "unit": latest.get("unit"),
                "prev_value": prev_value,
                "prev_period": prev.get("period") if prev else None,
                "change": change,
                "change_pct": change_pct,
                "rank": None,
                "n": 0,
                "flags": latest.get("flags") or [],
                "source": latest.get("source"),
                "updated_at": now,
            }
            if latest.get("payload") is not None:
                rec["payload"] = latest["payload"]
            records.append(rec)

        if not rank:
            return [undecimalize(self._put(r)) for r in records]
        ranked = sorted(
            (r for r in records if r["value"] is not None), key=lambda r: r["value"], reverse=True
        )
        n = len(ranked)
        prev_val = None
        prev_rank = 0
        for i, r in enumerate(ranked, start=1):
            prev_rank = prev_rank if (prev_val is not None and r["value"] == prev_val) else i
            prev_val = r["value"]
            r["rank"] = prev_rank
        for r in records:
            r["n"] = n
        return [undecimalize(self._put(r)) for r in records]

    def rebuild_snapshot(
        self,
        iso: str,
        indicator_ids: Sequence[str],
        doc_types: Sequence[str] = DEFAULT_DOC_TYPES,
    ) -> dict:
        """국가 프로필 1건(`SNAPSHOT#<iso>` / PROFILE)을 다시 만든다."""
        iso = iso.upper()
        latest: dict[str, Any] = {}
        for ind in indicator_ids:
            item = self.table.get_item(Key={"pk": latest_pk(ind), "sk": iso}).get("Item")
            if not item:
                continue
            row = undecimalize(item)
            row.pop("pk", None)
            row.pop("sk", None)
            latest[ind] = row
        docs: dict[str, Any] = {}
        for t in doc_types:
            doc = self.latest_doc(t, iso)
            if doc:
                docs[t] = {k: doc[k] for k in _DOC_SUMMARY_FIELDS if k in doc}
        item = {
            "pk": snapshot_pk(iso),
            "sk": "PROFILE",
            "iso": iso,
            "latest": latest,
            "docs": docs,
            "updated_at": self._now_iso(),
        }
        return undecimalize(self._put(item))

    def get_snapshot(self, iso: str) -> dict | None:
        item = self.table.get_item(Key={"pk": snapshot_pk(iso), "sk": "PROFILE"}).get("Item")
        return undecimalize(item) if item else None

    def get_latest(self, indicator: str, isos: Sequence[str] | None = None) -> list[dict]:
        """개요 히트맵용: 지표 1개의 LATEST 전체(또는 지정 국가만)를 Query 1회로 읽는다."""
        rows = self._query_all(Key("pk").eq(latest_pk(indicator)))
        out = [undecimalize(r) for r in rows]
        if isos is not None:
            want = {i.upper() for i in isos}
            out = [r for r in out if r.get("sk") in want]
        return out

    # ----------------------------------------------------- 수집 로그 (INGEST#)

    def log_ingest(self, source: str, summary: dict) -> dict:
        """수집 실행 결과를 기록한다 (`<run_ts>` 1건 + `LATEST` 1건).

        summary에 cadence_days가 있으면 `next_due`를 넣어 다음 실행의 케이던스 판단
        (`get_ingest_latest`)에 쓴다. 규칙(CONTRACT 12장):
          - next_due = (finished_at의 날짜 + cadence_days)의 **00:00 UTC**. 매일 21:00 UTC 고정
            기동에서 daily 소스가 격일로 밀리지 않도록 시각이 아니라 날짜 경계로 판정한다.
          - status가 failed/partial이면 cadence_days는 그대로 두고 next_due만 다음 날 00:00 UTC로
            앞당겨 다음 기동에서 재시도한다(`retry_reason` 기록).
        """
        now = self._now_iso()
        summary = dict(summary or {})
        finished_at = str(summary.pop("finished_at", None) or now)
        run_ts = str(summary.pop("run_ts", None) or finished_at)
        base = {
            "source": source,
            "run_ts": run_ts,
            "started_at": summary.pop("started_at", now),
            "finished_at": finished_at,
            "status": summary.pop("status", "ok"),
            **summary,
        }
        self._put({**base, "pk": ingest_pk(source), "sk": run_ts, "updated_at": now})
        latest = {**base, "pk": ingest_pk(source), "sk": "LATEST", "updated_at": now}
        cadence = summary.get("cadence_days")
        if cadence is not None:
            status = str(base.get("status") or "")
            if status in RETRY_STATUSES:
                latest["next_due"] = _next_due(finished_at, RETRY_DAYS)
                latest["retry_reason"] = f"status={status} → {RETRY_DAYS}일 뒤 재시도"
            else:
                latest["next_due"] = _next_due(finished_at, cadence)
        return undecimalize(self._put(latest))

    def get_ingest_latest(self, source: str) -> dict | None:
        item = self.table.get_item(Key={"pk": ingest_pk(source), "sk": "LATEST"}).get("Item")
        return undecimalize(item) if item else None

    def list_ingest(self, source: str, limit: int = 20) -> list[dict]:
        """실행 이력을 최신순으로 (`LATEST` 요약 항목은 제외)."""
        rows = self._query_all(
            Key("pk").eq(ingest_pk(source)), forward=False, limit=limit + 1
        )
        out = [undecimalize(r) for r in rows if r.get("sk") != "LATEST"]
        return out[:limit]

    # -------------------------------------------------------- 설정 (CONFIG)

    def get_config(self) -> dict:
        """LLM 예산 설정. 항목이 없으면 기본값, 날짜가 지났으면 사용량 0으로 정규화."""
        item = self.table.get_item(Key=CONFIG_KEY).get("Item") or {}
        today = self._today()
        cfg = {
            "llm_daily_token_budget": DEFAULT_LLM_DAILY_TOKEN_BUDGET,
            "llm_tokens_used_today": 0,
            "llm_tokens_date": today,
        }
        cfg.update({k: v for k, v in undecimalize(item).items() if k not in ("pk", "sk")})
        if str(cfg.get("llm_tokens_date")) != today:
            cfg["llm_tokens_date"] = today
            cfg["llm_tokens_used_today"] = 0
        return cfg

    def get_config_item(self) -> dict | None:
        """CONFIG#MACRO 항목을 기본값 합성 없이 그대로 (없으면 None). 예산 초기화 판단용."""
        item = self.table.get_item(Key=CONFIG_KEY).get("Item")
        return undecimalize(item) if item else None

    def set_config(self, **kwargs: Any) -> dict:
        if not kwargs:
            return self.get_config()
        sets = _decimalize(dict(kwargs))
        sets["updated_at"] = self._now_iso()
        names = {f"#k{i}": k for i, k in enumerate(sets)}
        values = {f":v{i}": v for i, v in enumerate(sets.values())}
        self.table.update_item(
            Key=CONFIG_KEY,
            UpdateExpression="SET " + ", ".join(f"#k{i} = :v{i}" for i in range(len(sets))),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        return self.get_config()

    def add_llm_tokens(self, n: int) -> int:
        """오늘 사용한 LLM 토큰을 누적하고 누적값을 돌려준다 (날짜가 바뀌면 리셋)."""
        n = int(n)
        today = self._today()
        now = self._now_iso()
        cur = self.table.get_item(Key=CONFIG_KEY).get("Item")
        if cur is None:
            item = {
                **CONFIG_KEY,
                "llm_daily_token_budget": Decimal(DEFAULT_LLM_DAILY_TOKEN_BUDGET),
                "llm_tokens_used_today": Decimal(n),
                "llm_tokens_date": today,
                "updated_at": now,
            }
            try:
                self.table.put_item(Item=item, ConditionExpression=Attr("pk").not_exists())
                return n
            except ClientError as e:
                if not _is_conditional_failure(e):
                    raise
                cur = self.table.get_item(Key=CONFIG_KEY).get("Item") or {}
        if str(cur.get("llm_tokens_date") or "") != today:
            try:
                resp = self.table.update_item(
                    Key=CONFIG_KEY,
                    UpdateExpression="SET #d = :today, #u = :n, updated_at = :ts",
                    # 다른 워커가 먼저 리셋했다면 조건이 깨지고 아래 ADD 경로로 내려간다.
                    ConditionExpression=Attr("llm_tokens_date").not_exists()
                    | Attr("llm_tokens_date").ne(today),
                    ExpressionAttributeNames={
                        "#d": "llm_tokens_date",
                        "#u": "llm_tokens_used_today",
                    },
                    ExpressionAttributeValues={":today": today, ":n": Decimal(n), ":ts": now},
                    ReturnValues="UPDATED_NEW",
                )
                return int(resp["Attributes"]["llm_tokens_used_today"])
            except ClientError as e:
                if not _is_conditional_failure(e):
                    raise
        resp = self.table.update_item(
            Key=CONFIG_KEY,
            UpdateExpression="SET updated_at = :ts ADD #u :n",
            ExpressionAttributeNames={"#u": "llm_tokens_used_today"},
            ExpressionAttributeValues={":n": Decimal(n), ":ts": now},
            ReturnValues="UPDATED_NEW",
        )
        return int(resp["Attributes"]["llm_tokens_used_today"])

    # ------------------------------------------------- 수집 락 (CONFIG / LOCK#collect)

    def acquire_lock(self, holder: str, *, ttl_seconds: int = LOCK_TTL_SECONDS) -> bool:
        """수집기 동시 실행 방지 락을 잡는다. 반환: 획득 여부.

        항목이 없거나 `expires_at`이 지났으면 조건부 put으로 가져온다
        (`attribute_not_exists(pk) OR expires_at < :now`). 정상 종료는 release_lock으로
        지우지만, 태스크가 죽어 남은 락은 TTL(기본 3시간)이 지나면 다음 기동이 넘겨받는다.
        """
        now = self._now_iso()
        now_dt = datetime.fromisoformat(now)
        expires = (now_dt + timedelta(seconds=int(ttl_seconds))).isoformat(timespec="seconds")
        item = {**LOCK_KEY, "holder": str(holder), "acquired_at": now, "expires_at": expires,
                "updated_at": now}
        try:
            self.table.put_item(
                Item=item,
                ConditionExpression=Attr("pk").not_exists() | Attr("expires_at").lt(now),
            )
        except ClientError as e:
            if _is_conditional_failure(e):
                return False
            raise
        return True

    def release_lock(self, holder: str) -> bool:
        """자기 락만 지운다(holder 일치 조건). 이미 다른 기동이 넘겨받았으면 False."""
        try:
            self.table.delete_item(
                Key=dict(LOCK_KEY),
                ConditionExpression=Attr("holder").eq(str(holder)),
            )
        except ClientError as e:
            if _is_conditional_failure(e):
                return False
            raise
        return True

    def get_lock(self) -> dict | None:
        """현재 락 항목(만료 여부 무관). 없으면 None."""
        item = self.table.get_item(Key=dict(LOCK_KEY)).get("Item")
        return undecimalize(item) if item else None

    def lock_active(self) -> dict | None:
        """만료 전인 락만 돌려준다 (없거나 만료면 None)."""
        lock = self.get_lock()
        if not lock:
            return None
        return lock if str(lock.get("expires_at") or "") > self._now_iso() else None

    # ------------------------------------------------------------- S3 (8장)

    def save_raw(self, source: str, name: str, data: bytes | str | dict) -> str | None:
        """원본 응답을 `macro/raw/<source>/<날짜>/<name>.json.gz`로 보존한다.

        s3가 없으면 local_dir 아래 같은 상대경로로 쓴다(dry-run). 실패하면 None.
        """
        key = f"macro/raw/{source}/{self._today()}/{_safe_name(name)}.json.gz"
        try:
            if isinstance(data, (dict, list)):
                raw = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
            elif isinstance(data, str):
                raw = data.encode("utf-8")
            elif isinstance(data, (bytes, bytearray, memoryview)):
                # bytes(int)는 0으로 채운 버퍼를 만들어 버리므로 타입을 명시적으로 검사한다.
                raw = bytes(data)
            else:
                raise TypeError(f"save_raw는 bytes|str|dict만 받는다: {type(data).__name__}")
            body = raw if raw[:2] == b"\x1f\x8b" else gzip.compress(raw)
            return self._write_object(
                key, body, content_type="application/json", content_encoding="gzip"
            )
        except Exception as e:  # noqa: BLE001 - 원본 보존 실패가 수집을 죽이지 않게
            logger.warning("원본 저장 실패 %s: %s", key, e)
            return None

    def save_doc_text(self, doc: Doc, text: str) -> str:
        """문서 전문을 `macro/docs/<type>/<iso>/<date>_<id>.md`로 저장하고 키를 돌려준다.

        저장 성공 시 doc.s3_key도 채워주므로 이어서 put_docs([doc])를 호출하면 된다.
        """
        key = f"macro/docs/{doc.type}/{doc.iso}/{doc.date}_{doc.id}.md"
        self._write_object(
            key, text.encode("utf-8"), content_type="text/markdown; charset=utf-8"
        )
        doc.s3_key = key
        return key

    def _write_object(
        self,
        key: str,
        body: bytes,
        *,
        content_type: str | None = None,
        content_encoding: str | None = None,
    ) -> str:
        if self.s3 is not None and self.bucket:
            kw: dict[str, Any] = {"Bucket": self.bucket, "Key": key, "Body": body}
            if content_type:
                kw["ContentType"] = content_type
            if content_encoding:
                kw["ContentEncoding"] = content_encoding
            self.s3.put_object(**kw)
            return key
        path = self.local_dir / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        logger.info("로컬 저장 %s", path)
        return key


# ------------------------------------------------------------- 키 헬퍼 (4장)


def obs_pk(indicator: str, iso: str) -> str:
    return f"OBS#{indicator}#{iso.upper()}"


def latest_pk(indicator: str) -> str:
    return f"LATEST#{indicator}"


def snapshot_pk(iso: str) -> str:
    return f"SNAPSHOT#{iso.upper()}"


def doc_pk(type: str, iso: str) -> str:  # noqa: A002
    return f"DOC#{type}#{iso.upper()}"


def ingest_pk(source: str) -> str:
    return f"INGEST#{source}"


def _same_observed_value(old: dict, new_item: dict) -> bool:
    """개정 여부 판정: value(6자리 반올림)와 payload가 모두 같으면 변경 없음."""
    return undecimalize(old.get("value")) == undecimalize(new_item.get("value")) and (
        undecimalize(old.get("payload")) == undecimalize(new_item.get("payload"))
    )


def _next_due(finished_at: str, cadence_days: Any) -> str:
    """(finished_at의 UTC 날짜 + cadence_days) 00:00 UTC. 판정은 `now >= next_due`."""
    try:
        base = datetime.fromisoformat(str(finished_at))
    except ValueError:
        base = datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    base = base.astimezone(timezone.utc)
    due_day = base.date() + timedelta(days=int(float(cadence_days)))
    due = datetime(due_day.year, due_day.month, due_day.day, tzinfo=timezone.utc)
    return due.isoformat(timespec="seconds")


def _safe_name(name: str) -> str:
    return _SAFE_NAME_RE.sub("_", str(name)).strip("_") or "raw"
