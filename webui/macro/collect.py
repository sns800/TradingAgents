#!/usr/bin/env python3
# ============================================================
# [모듈 개요] G20 매크로 수집기 오케스트레이터 — Fargate 진입점 (CONTRACT.md 12장)
#
#   python webui/macro/collect.py [--sources a,b] [--countries KR,US] [--since YYYY-MM-DD]
#                                 [--dry-run] [--dump PATH] [--load PATH] [--no-llm|--llm]
#                                 [--force] [--skip-derived] [--skip-rebuild] [--log-level L]
#
# 실행 순서 (CONTRACT 12장 · 소스 모듈은 수집만, 우선순위·폴백·파생·저장은 전부 이 파일)
#   1) 진입: sys.path에 webui/ 추가, socket 기본 타임아웃(yfinance 무타임아웃 대비), 로깅
#      운영이면 boto3 DynamoDB/S3(HOME_REGION), dry-run이면 FakeTable/FakeS3 + 로컬 .macro_raw/
#   2) SERIES#/META 갱신 (registry.to_meta_items — 매 실행)
#   2b) 동시 실행 가드: `CONFIG / LOCK#collect` 조건부 put(만료 3시간). 실패면 "다른 수집 실행 중"
#       로그 후 exit 0. 종료(finally)에 자기 락만 삭제.
#   3) CollectContext 구성 (catalog_loader·fx_rates·seen_urls·seen_hashes·llm)
#   4) 소스 루프 (정량 → LLM): 케이던스 판단(INGEST#<source>/LATEST.next_due) → 소스별 try/except 격리
#   5) 우선순위 병합: 같은 (indicator, iso)에서 registry sorted_sources() 상위 소스가 관측을 냈으면
#      하위 소스의 같은/더 낮은 빈도는 버리고, 상위가 만들 수 없는 고빈도 보충만 남긴다.
#      상위 소스가 이번 실행에 참여했는데 그 (indicator, iso)를 하나도 못 냈으면 하위 소스를
#      채택하고 `fallback_source`를 붙인다. 특례: fx_usd 월별은 BIS가 없으면 yahoo 일별을 D→M 기말 집계,
#      US fx_usd는 기준통화라 어떤 소스도 내지 않으므로 1.0을 합성(synth_us_fx_monthly).
#   6) 소스별 put_observations (INGEST 요약의 n_new/n_updated)
#   7) 파생: *_yoy(yoy_from 지수, 직접 관측이 있으면 생략) · fx_value_index(1년 전 대비 M, Q/Y는 집계)
#      · 저빈도 집계(store_freqs, agg=indicator.agg, 이번 실행이 건드린 버킷만)
#      증분 실행(ctx.since 有)이면 파생도 이번 실행이 건드린 (지표, 국가)만, 조회 창은 since − 시차.
#   8) poll_of_polls 문서 + party_support/gov_approval 월 관측 (이번 달)
#   9) LATEST/SNAPSHOT 재생성 (전 지표 × 전 국가 — 순위 일관성)
#  10) weekly_brief (LATEST 결과 + 최근 7일 문서) → INGEST 기록 → 요약 표 → 종료 코드
#
# 규칙
#  - 유로 회원국(DE/FR/IT)의 공유 지표(policy_rate/m2_*/fx_*)는 수집·파생·LATEST 모두 만들지 않는다.
#  - AWS 쓰기는 운영 모드에서만. dry-run은 인메모리 테이블·S3, 원본은 ./.macro_raw/.
#  - 소스 모듈 목록은 SOURCE_MODULES(dict[str, module])로 노출해 테스트가 가짜 모듈로 바꿔 끼운다.
#  - 종료 코드: 실행한 소스가 전부 failed면 1, 아니면 0 (부분 실패는 INGEST status=partial).
#  - 후처리(병합·파생·집계·poll_of_polls·재생성·INGEST)는 단계별 try/except로 격리 — 한 단계가
#    죽어도 다음 단계(특히 LATEST 재생성·INGEST 기록)는 실행되고 실패는 post_errors로 남긴다.
# ============================================================
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import logging
import os
import socket
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

WEBUI_DIR = Path(__file__).resolve().parents[1]
if str(WEBUI_DIR) not in sys.path:
    sys.path.insert(0, str(WEBUI_DIR))

from macro import aggregate  # noqa: E402
from macro.fakeddb import FakeS3, FakeTable  # noqa: E402
from macro.llm import cb_statements, energy_policy, polls_wiki, weekly_brief  # noqa: E402
from macro.llm.bedrock_json import BedrockJson  # noqa: E402
from macro.llm.meta import polls_meta  # noqa: E402
from macro.registry import Indicator, Registry, load_registry, validate  # noqa: E402
from macro.schema import Doc, Observation, _decimalize, now_iso, undecimalize  # noqa: E402
from macro.sources import (  # noqa: E402
    bis,
    companies,
    ember,
    fred,
    imf,
    oecd,
    owid,
    wits,
    worldbank,
    yahoo_fx,
)
from macro.sources.base import EURO_SHARED_INDICATORS, CollectContext  # noqa: E402
from macro.store import DEFAULT_FREQ_PREF, LOCK_KEY, MacroStore  # noqa: E402

logger = logging.getLogger("macro.collect")

# CONTRACT 12장 실행 순서: 정량 소스 → LLM 소스. weekly_brief는 LATEST 재생성 뒤의 후처리.
QUANT_ORDER: tuple[str, ...] = (
    "yahoo",
    "bis",
    "oecd",
    "imf",
    "fred",
    "ember",
    "owid",
    "worldbank",
    "wits",
    "companies",
)
LLM_ORDER: tuple[str, ...] = ("cb_statements", "wiki_polls", "energy_policy")
BRIEF_SOURCE = "weekly_brief"
DEFAULT_SOURCES: tuple[str, ...] = QUANT_ORDER + LLM_ORDER + (BRIEF_SOURCE,)

# 소스 이름(INGEST 식별자) → 모듈. 테스트는 이 dict를 monkeypatch한다.
SOURCE_MODULES: dict[str, ModuleType] = {
    "yahoo": yahoo_fx,
    "bis": bis,
    "oecd": oecd,
    "imf": imf,
    "fred": fred,
    "ember": ember,
    "owid": owid,
    "worldbank": worldbank,
    "wits": wits,
    "companies": companies,
    "cb_statements": cb_statements,
    "wiki_polls": polls_wiki,
    "energy_policy": energy_policy,
}
LLM_SOURCES = frozenset(LLM_ORDER)
CADENCE_DAYS: dict[str, int] = {"daily": 1, "weekly": 7, "monthly": 30, "quarterly": 90}

FREQ_RANK = aggregate.FREQ_RANK
DERIVED_SOURCE = "derived"
DRY_RUN_BUCKET = "dry-run"
# --since 생략 시 증분 시작일 = 선택 소스 중 가장 오래된 마지막 실행일 − 이 일수 (개정·지연 흡수)
INCREMENTAL_LOOKBACK_DAYS = 45
POLL_WINDOW_DAYS = 30
POLL_DOC_LIMIT = 60
BRIEF_RECENT_DAYS = 7
BRIEF_DOC_TYPES: tuple[str, ...] = ("cb_stance", "energy_policy", "poll_of_polls", "election")
SEEN_CB_DOCS_PER_COUNTRY = 30
FX_VALUE_METHOD = "1년 전 대비 통화가치 지수 (100=변동 없음, 환율=현지통화/USD)"
# 미국 환율 기준값 합성 (CONTRACT 12장 폴백 체인): 어떤 소스도 US fx_usd를 내지 않는다
US_FX_ISO = "US"
US_FX_SERIES_ID = "USD_BASE"
US_FX_SOURCE_URL = "https://www.federalreserve.gov/releases/h10/"
US_FX_METHOD = "기준통화(USD) = 1.0 (합성값)"
# 병합 시 유효한 소스 값을 가진 이전 관측을 찾을 때 사용하는 날짜 파서 실패 방어용
_MAX_ERRORS_LOGGED = 50
# 통화가치 지수는 12개월 전 환율이 필요하다 (증분 실행의 조회 창 확장 폭)
FX_VALUE_LAG_MONTHS = 12
# 단위가 국가별로 다른 지표는 LATEST rank를 만들지 않는다 (CONTRACT 2장 lcu_bn)
NO_RANK_UNITS = frozenset({"lcu_bn"})


# ------------------------------------------------------------------ 옵션/실행 기록
@dataclass
class Options:
    """CLI 인자와 1:1. 테스트는 이 객체를 직접 만들어 Collector에 넘긴다."""

    sources: list[str] = field(default_factory=lambda: list(DEFAULT_SOURCES))
    countries: list[str] = field(default_factory=list)  # 비면 레지스트리 20개국
    since: date | None = None
    dry_run: bool = False
    no_llm: bool = False
    force: bool = False
    skip_derived: bool = False
    skip_rebuild: bool = False
    dump: str | None = None
    load: str | None = None


@dataclass
class SourceRun:
    """소스 1개의 실행 기록 (INGEST 요약 + 마지막 요약 표의 재료)."""

    name: str
    cadence: str
    started_at: str
    finished_at: str = ""
    status: str = "ok"  # ok | partial | failed | skipped
    note: str = ""
    targets: list[str] = field(default_factory=list)
    raw_obs: list[Observation] = field(default_factory=list)
    docs: list[Doc] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    n_obs: int = 0  # 병합 후 저장을 시도한 관측 수
    put: dict[str, int] = field(default_factory=dict)
    n_docs: int = 0
    elapsed: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)  # INGEST 요약에 덧붙일 필드(hashes 등)

    @property
    def ran(self) -> bool:
        return self.status != "skipped"

    def failed_isos(self) -> list[str]:
        out: list[str] = []
        for e in self.errors:
            parts = e.split(":", 2)
            iso = parts[1].strip() if len(parts) >= 2 else ""
            if iso and iso != "*" and iso not in out:
                out.append(iso)
        return out


class _ProbeSet(set):
    """energy_policy가 `sha1 in seen_hashes`로 조회한 원문 해시를 기록하는 집합 (구버전 호환).

    지금은 모듈이 `ctx.extra["energy_hashes"] = {iso: sha1}`을 직접 남기므로 이 우회로는
    쓰이지 않는다. 그 키를 남기지 않는 모듈(가짜 모듈·구버전)을 만나면 조회된 후보 해시를
    생성된 문서 id(`sha12(f"{iso}:{sha1}")`)와 맞춰 `{iso: sha1}`을 복원한다.
    """

    def __init__(self, items: Iterable[str] = ()) -> None:
        super().__init__(items)
        self.probed: list[str] = []

    def __contains__(self, item: object) -> bool:
        if isinstance(item, str) and item not in self.probed:
            self.probed.append(item)
        return super().__contains__(item)


# ------------------------------------------------------------------ 순수 함수 (테스트 대상)
def merge_observations(
    registry: Registry,
    buckets: dict[str, list[Observation]],
    *,
    ran_sources: Iterable[str] | None = None,
) -> dict[str, list[Observation]]:
    """소스별 관측 버킷을 registry 우선순위로 병합한다 (CONTRACT 12장 폴백 체인).

    반환은 `{소스 모듈 이름: 채택된 관측 목록}` — 소스별로 put_observations를 나눠 호출해
    INGEST 요약에 n_new/n_updated를 남기기 위함이다.

    규칙 (같은 (indicator, iso) 안에서)
    - primary = sorted_sources() 순서로 가장 앞선, 관측을 낸 소스. 그 관측은 모두 채택.
    - 하위 소스는 primary가 만든 가장 고빈도보다 **더 고빈도**인 관측만 보충으로 채택한다
      (예: BIS fx_usd M이 있어도 yahoo D는 유지). 같은/더 낮은 빈도는 primary(또는 그 집계)가 덮으므로 버린다.
    - primary보다 우선순위가 높은 소스가 **이번 실행에 참여했는데** 그 (indicator, iso)를 못 냈으면
      채택된 관측 전부에 `fallback_source`를 붙인다. 참여하지 않은 소스(예: --sources yahoo만)는
      판단 근거가 없으므로 플래그를 붙이지 않는다.
    - 유로 회원국의 공유 지표(policy_rate/m2_*/fx_*)는 소스가 내더라도 버린다.
    """
    ran = set(ran_sources) if ran_sources is not None else set(buckets)
    grouped: dict[tuple[str, str], dict[str, dict[str, list[Observation]]]] = {}
    for module, obs_list in buckets.items():
        for o in obs_list:
            grouped.setdefault((o.indicator, o.iso), {}).setdefault(module, {}).setdefault(
                o.freq, []
            ).append(o)

    out: dict[str, list[Observation]] = {m: [] for m in buckets}
    for (ind_id, iso), by_module in grouped.items():
        if _is_euro_member_shared(registry, ind_id, iso):
            continue
        indicator = registry.indicator(ind_id) if registry.has_indicator(ind_id) else None
        order = _module_order(indicator, list(by_module))
        primary = order[0]
        primary_finest = min(FREQ_RANK[f] for f in by_module[primary])
        flag = _has_missing_higher_source(registry, indicator, iso, primary, ran)
        for module in order:
            for freq, obs_list in by_module[module].items():
                if module != primary and FREQ_RANK[freq] >= primary_finest:
                    continue  # 하위 소스의 같은/더 낮은 빈도 → primary(집계)가 덮는다
                for o in obs_list:
                    if flag and "fallback_source" not in o.flags:
                        o.flags.append("fallback_source")
                    out[module].append(o)
    return out


def _module_order(indicator: Indicator | None, present: list[str]) -> list[str]:
    """관측을 낸 모듈들을 registry 우선순위 순으로. 등록되지 않은 모듈은 뒤에 붙인다."""
    names = indicator.source_names() if indicator is not None else []
    ranked = [n for n in names if n in present]
    ranked += [m for m in present if m not in ranked]
    return ranked


def _has_missing_higher_source(
    registry: Registry,
    indicator: Indicator | None,
    iso: str,
    primary: str,
    ran: set[str],
) -> bool:
    """primary보다 앞선 소스 중, 이번 실행에 참여했고 이 국가를 담당하는데 값을 못 낸 소스가 있는가."""
    if indicator is None:
        return False
    for name in indicator.source_names():
        if name == primary:
            return False
        if name == DERIVED_SOURCE or name not in ran:
            continue
        try:
            covered = {c.iso for c in registry.countries_for_source(name, indicator.id)}
        except ValueError:
            covered = set()
        if iso in covered:
            return True
    return False


def _is_euro_member_shared(registry: Registry, indicator_id: str, iso: str) -> bool:
    return (
        indicator_id in EURO_SHARED_INDICATORS
        and registry.has_country(iso)
        and registry.country(iso).euro
    )


def fill_fx_month_from_daily(
    daily: list[Observation], today: date | None = None
) -> list[Observation]:
    """BIS 월별 환율이 없을 때 yahoo 일별을 D→M 기말 집계해 fallback으로 채운다."""
    if not daily:
        return []
    monthly = aggregate.to_lower_freq(daily, "M", "last", today=today)
    for o in monthly:
        if "fallback_source" not in o.flags:
            o.flags.append("fallback_source")
        o.method += " · BIS 월별 부재로 yahoo 일별을 기말 집계한 폴백"
    return monthly


def synth_us_fx_monthly(
    other_monthly: Iterable[Observation], *, retrieved_at: str | None = None
) -> list[Observation]:
    """US `fx_usd` 월별 관측을 1.0으로 합성한다 (정의상 기준통화이나 어떤 소스도 내지 않는다).

    기간 범위는 이번 실행의 **다른 국가** fx_usd 월별 관측의 최소~최대 월이며, 그 사이
    모든 월을 채운다(중간 달이 빠지면 fx_value_index·저빈도 집계에 구멍이 생긴다).
    일별(D)은 만들지 않는다 — 통화가치 지수는 월별에서만 파생한다.
    """
    periods = [o.period for o in other_monthly if o.freq == "M" and o.iso != US_FX_ISO]
    if not periods:
        return []
    lo, hi = min(periods), max(periods)
    stamp = retrieved_at or now_iso()
    out: list[Observation] = []
    for period in aggregate.periods_between("M", lo, hi):
        out.append(
            Observation(
                indicator="fx_usd",
                iso=US_FX_ISO,
                freq="M",
                period=period,
                value=1.0,
                unit="lcu_per_usd",
                source=DERIVED_SOURCE,
                series_id=US_FX_SERIES_ID,
                source_url=US_FX_SOURCE_URL,
                method=US_FX_METHOD,
                retrieved_at=stamp,
                flags=["derived"],
            )
        )
    return out


def fx_value_index_yearly(fx_monthly: list[Observation]) -> list[Observation]:
    """통화가치 지수(월) = fx[t−12] / fx[t] × 100 (100=1년 전과 같음, 값↑=통화 강세)."""
    if not fx_monthly:
        return []
    by_period = {o.period: o for o in fx_monthly if o.value}
    out: list[Observation] = []
    for o in sorted(fx_monthly, key=lambda x: aggregate.period_sort_key(x.freq, x.period)):
        if not o.value or float(o.value) <= 0:
            continue
        base_period = aggregate.previous_period("M", o.period, 12)
        base = by_period.get(base_period)
        if base is None or not base.value or float(base.value) <= 0:
            continue
        flags = ["derived"] + [f for f in o.flags if f not in ("derived", "partial_period")]
        out.append(
            Observation(
                indicator="fx_value_index",
                iso=o.iso,
                freq="M",
                period=o.period,
                value=float(base.value) / float(o.value) * 100.0,
                unit="index",
                source=DERIVED_SOURCE,
                series_id=f"fx_usd@{o.source}",
                source_url=o.source_url,
                method=f"{FX_VALUE_METHOD} = {base_period} 환율 / {o.period} 환율 × 100",
                vintage=o.vintage,
                flags=flags,
            )
        )
    return out


def obs_from_item(item: dict[str, Any]) -> Observation:
    """DynamoDB OBS 항목(undecimalize 완료) → Observation."""
    return Observation(
        indicator=str(item["indicator"]),
        iso=str(item["iso"]),
        freq=str(item["freq"]),
        period=str(item["period"]),
        value=None if item.get("value") is None else float(item["value"]),
        unit=str(item.get("unit") or ""),
        source=str(item.get("source") or ""),
        series_id=str(item.get("series_id") or ""),
        source_url=str(item.get("source_url") or ""),
        method=str(item.get("method") or ""),
        payload=item.get("payload"),
        vintage=str(item.get("vintage") or item.get("retrieved_at", "")[:10] or date.today()),
        retrieved_at=str(item.get("retrieved_at") or now_iso()),
        flags=[f for f in (item.get("flags") or []) if isinstance(f, str)],
    )


def doc_from_item(item: dict[str, Any]) -> Doc:
    """DynamoDB DOC 항목 → Doc (aggregate.poll_of_polls 입력용)."""
    return Doc(
        type=str(item["type"]),
        iso=str(item["iso"]),
        date=str(item["date"]),
        id=str(item["id"]),
        title_ko=str(item.get("title_ko") or ""),
        summary_ko=str(item.get("summary_ko") or ""),
        source_url=str(item.get("source_url") or ""),
        source_name=str(item.get("source_name") or ""),
        payload=dict(item.get("payload") or {}),
        ai_generated=bool(item.get("ai_generated")),
        model_id=item.get("model_id"),
        confidence=item.get("confidence"),
        quotes=[str(q) for q in (item.get("quotes") or [])],
        review_status=str(item.get("review_status") or "approved"),
        reviewed_by=item.get("reviewed_by"),
        reviewed_at=item.get("reviewed_at"),
        s3_key=item.get("s3_key"),
        retrieved_at=str(item.get("retrieved_at") or now_iso()),
    )


def dump_state(table: Any, s3: Any, path: str | Path) -> Path:
    """dry-run 테이블·S3 내용을 JSON 한 파일로 저장한다 (devserver --load / collect --load)."""
    items = [
        undecimalize(it)
        for it in getattr(table, "items", {}).values()
        # 실행 중 잡고 있는 락은 덤프에 남기지 않는다 (--load 시 다음 dry-run이 스킵되지 않게)
        if not (it.get("pk") == LOCK_KEY["pk"] and it.get("sk") == LOCK_KEY["sk"])
    ]
    objects: dict[str, Any] = {}
    for key, rec in (getattr(s3, "objects", None) or {}).items():
        objects[key] = {
            "body_b64": base64.b64encode(rec["Body"]).decode("ascii"),
            "content_type": rec.get("ContentType"),
            "content_encoding": rec.get("ContentEncoding"),
        }
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {"version": 1, "dumped_at": now_iso(), "table": items, "s3": objects},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return out


def load_state(path: str | Path, table: Any, s3: Any | None = None) -> int:
    """dump_state 산출물을 FakeTable/FakeS3에 되살린다. 반환: 복원한 테이블 항목 수."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    n = 0
    for item in raw.get("table") or []:
        table.put_item(Item=_decimalize(item))
        n += 1
    if s3 is not None:
        for key, rec in (raw.get("s3") or {}).items():
            s3.put_object(
                Bucket=DRY_RUN_BUCKET,
                Key=key,
                Body=base64.b64decode(rec.get("body_b64") or ""),
                ContentType=rec.get("content_type"),
                ContentEncoding=rec.get("content_encoding"),
            )
    return n


# ------------------------------------------------------------------ 저장소 구성
def build_store(opts: Options) -> tuple[MacroStore, Callable[[str, str, Any], str | None]]:
    """(store, raw_saver). dry-run은 인메모리 + 로컬 원본, 운영은 DynamoDB/S3(HOME_REGION)."""
    if opts.dry_run:
        table, s3 = FakeTable(), FakeS3()
        if opts.load:
            n = load_state(opts.load, table, s3)
            logger.info("덤프 복원 %s — 테이블 %d건, S3 %d건", opts.load, n, len(s3.objects))
        store = MacroStore(table, s3=s3, bucket=DRY_RUN_BUCKET)
        # 원본 응답은 인메모리 S3가 아니라 로컬 ./.macro_raw/ 에 남긴다 (덤프 크기 억제)
        return store, MacroStore(table).save_raw

    import boto3

    region = os.environ.get("HOME_REGION", "ap-northeast-2")
    table = boto3.resource("dynamodb", region_name=region).Table(os.environ["MACRO_TABLE_NAME"])
    s3 = boto3.client("s3", region_name=region)
    store = MacroStore(table, s3=s3, bucket=os.environ["DATA_BUCKET"])
    return store, store.save_raw


# ------------------------------------------------------------------ 수집기
class Collector:
    """한 번의 배치 실행. run()이 종료 코드를 돌려준다."""

    def __init__(
        self,
        opts: Options,
        *,
        store: MacroStore,
        raw_saver: Callable[[str, str, Any], str | None] | None = None,
        registry: Registry | None = None,
        today: date | None = None,
    ) -> None:
        self.opts = opts
        self.store = store
        self.raw_saver = raw_saver or store.save_raw
        self.registry = registry or load_registry()
        validate(self.registry)
        self.today = today or datetime.now(timezone.utc).date()
        self.runs: dict[str, SourceRun] = {}
        self.buckets: dict[str, list[Observation]] = {}
        self.merged: dict[str, list[Observation]] = {}
        self.derived_stats: dict[str, int] = {}
        self.latest_rows: list[dict[str, Any]] = []
        self.n_latest = 0
        self.n_snapshot = 0
        self.ctx: CollectContext | None = None
        self.llm_created = False
        self._energy_hashes: dict[str, str] = {}
        self._selected: list[str] = []
        self._isos: list[str] = []
        self.run_ts = now_iso()
        self.post_errors: list[str] = []  # 후처리 단계 실패 (INGEST post_errors로 기록)
        self.lock_skipped = False

    # ------------------------------------------------------------ 진입
    def run(self) -> int:
        t0 = time.monotonic()
        self._selected = self._selected_sources()
        self._isos = self._selected_isos()
        logger.info(
            "수집 시작 — 소스 %s · 국가 %s · dry_run=%s · no_llm=%s · force=%s",
            ",".join(self._selected),
            ",".join(self._isos),
            self.opts.dry_run,
            self.opts.no_llm,
            self.opts.force,
        )
        if not self.store.acquire_lock(self.run_ts):
            lock = self.store.get_lock() or {}
            self.lock_skipped = True
            logger.warning(
                "다른 수집이 실행 중 — 이번 기동은 건너뜀 (holder %s · expires_at %s)",
                lock.get("holder"),
                lock.get("expires_at"),
            )
            return 0
        try:
            return self._run_locked(t0)
        finally:
            try:
                self.store.release_lock(self.run_ts)
            except Exception:  # noqa: BLE001 - 락 해제 실패는 TTL이 흡수한다
                logger.exception("수집 락 해제 실패 (TTL 만료 후 자동 회수)")

    def _run_locked(self, t0: float) -> int:
        n_meta = self.store.put_meta_items(self.registry.to_meta_items())
        logger.info("지표 사전 갱신 %d건 (SERIES#/META)", n_meta)

        since = self._resolve_since()
        ctx = CollectContext(
            since=since,
            dry_run=self.opts.dry_run,
            no_llm=self.opts.no_llm,
            raw_saver=self.raw_saver,
            log=logger.info,
        )
        self.ctx = ctx
        ctx.extra["catalog_loader"] = self._catalog_loader()
        self._prepare_llm(ctx)

        for name in self._selected:
            if name == BRIEF_SOURCE:
                continue
            self._run_source(name)

        # 후처리는 단계별로 격리 — 앞 단계가 죽어도 LATEST 재생성·INGEST 기록은 실행한다
        self._stage("merge_put", self._merge_and_put)
        if not self.opts.skip_derived:
            self._stage("derive", self._derive)
        else:
            logger.info("파생·집계 건너뜀 (--skip-derived)")
        self._stage("poll_of_polls", self._poll_of_polls)
        if not self.opts.skip_rebuild:
            self._stage("rebuild", self._rebuild)
        else:
            logger.info("LATEST/SNAPSHOT 재생성 건너뜀 (--skip-rebuild)")
        if BRIEF_SOURCE in self._selected:
            self._stage("weekly_brief", self._weekly_brief)
        self._stage("log_ingest", self._log_ingest)
        if self.post_errors:
            logger.warning("후처리 실패 %d건 — %s", len(self.post_errors), " | ".join(self.post_errors))
        if self.opts.dump:
            path = dump_state(self.store.table, self.store.s3, self.opts.dump)
            logger.info("덤프 저장 %s (%.1f KB)", path, path.stat().st_size / 1024)
        self._summary(time.monotonic() - t0)

        ran = [r for r in self.runs.values() if r.ran]
        return 1 if ran and all(r.status == "failed" for r in ran) else 0

    def _stage(self, name: str, fn: Callable[[], Any]) -> bool:
        """후처리 한 단계를 격리 실행한다. 실패는 post_errors에 남기고 False를 돌려준다."""
        try:
            fn()
            return True
        except Exception as exc:  # noqa: BLE001 - 단계 단위 실패 격리
            logger.exception("[후처리 %s] 실패 — 다음 단계로 진행", name)
            self.post_errors.append(f"{name}: {type(exc).__name__}: {exc}")
            return False

    # ------------------------------------------------------------ 선택/준비
    def _selected_sources(self) -> list[str]:
        wanted = [s.strip() for s in self.opts.sources if s.strip()] or list(DEFAULT_SOURCES)
        known = set(SOURCE_MODULES) | {BRIEF_SOURCE}
        unknown = [s for s in wanted if s not in known]
        if unknown:
            raise ValueError(f"알 수 없는 소스: {unknown} (가능: {sorted(known)})")
        # 실행 순서는 CONTRACT 12장 순서를 따르고, 등록 순서에 없는 가짜 모듈은 뒤에 붙인다.
        ordered = [s for s in DEFAULT_SOURCES if s in wanted]
        ordered += [s for s in wanted if s not in ordered]
        return ordered

    def _selected_isos(self) -> list[str]:
        if not self.opts.countries:
            return list(self.registry.iso_codes)
        isos = [c.strip().upper() for c in self.opts.countries if c.strip()]
        unknown = [i for i in isos if not self.registry.has_country(i)]
        if unknown:
            raise ValueError(f"알 수 없는 국가 코드: {unknown}")
        return [c.iso for c in self.registry.countries if c.iso in set(isos)]

    def _resolve_since(self) -> date | None:
        """--since가 없으면 증분 시작일을 INGEST 기록에서 정한다 (기록 없는 소스가 있으면 전체)."""
        if self.opts.since:
            logger.info("증분 시작일 %s (--since)", self.opts.since)
            return self.opts.since
        finished: list[date] = []
        for name in self._selected:
            if name == BRIEF_SOURCE:
                continue
            latest = self.store.get_ingest_latest(name)
            stamp = _parse_dt((latest or {}).get("finished_at"))
            if stamp is None:
                logger.info("증분 시작일 없음 — %s 첫 실행이라 소스 기본 시작일(전체)로 수집", name)
                return None
            finished.append(stamp.date())
        if not finished:
            return None
        since = min(finished) - timedelta(days=INCREMENTAL_LOOKBACK_DAYS)
        logger.info("증분 시작일 %s (마지막 실행 %s − %d일)", since, min(finished), INCREMENTAL_LOOKBACK_DAYS)
        return since

    def _catalog_loader(self) -> Callable[[str], list[dict] | None] | None:
        """S3 `catalog/{market}.json.gz`의 items 로더. dry-run/실패 시 None → companies가 yfinance 폴백."""
        if self.opts.dry_run or self.store.s3 is None or not self.store.bucket:
            return None
        s3, bucket = self.store.s3, self.store.bucket

        def loader(market: str) -> list[dict] | None:
            key = f"catalog/{market}.json.gz"
            try:
                body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
                if body[:2] == b"\x1f\x8b":
                    body = gzip.decompress(body)
                data = json.loads(body.decode("utf-8"))
                items = data.get("items") if isinstance(data, dict) else data
                return list(items or [])
            except Exception as exc:  # noqa: BLE001 - 카탈로그 부재는 폴백 사유일 뿐
                logger.warning("카탈로그 로드 실패 %s: %s", key, exc)
                return None

        return loader

    def _prepare_llm(self, ctx: CollectContext) -> None:
        """LLM 핸들·예산·변경 감지 힌트(seen_urls/seen_hashes)를 ctx에 넣는다."""
        needs = any(s in LLM_SOURCES or s == BRIEF_SOURCE for s in self._selected)
        if not needs:
            return
        if not self.opts.no_llm:
            budget = self._llm_budget()
            store = self.store
            ctx.llm = BedrockJson(
                budget_check=lambda est: store.get_config()["llm_tokens_used_today"] + est <= budget,
                on_tokens=store.add_llm_tokens,
            )
            self.llm_created = True
            logger.info("LLM 사용 — 모델 %s · 일일 예산 %d 토큰", getattr(ctx.llm, "model_id", "?"), budget)
        else:
            logger.info("LLM 비활성(no_llm) — 정성 소스는 원문만 수집")
        ctx.extra["seen_urls"] = self._seen_cb_urls()
        prev = self.store.get_ingest_latest("energy_policy") or {}
        hashes = prev.get("hashes") if isinstance(prev.get("hashes"), dict) else {}
        self._energy_hashes = {str(k): str(v) for k, v in hashes.items()}
        ctx.extra["seen_hashes"] = _ProbeSet(self._energy_hashes.values())

    def _llm_budget(self) -> int:
        """일일 LLM 토큰 예산. CONFIG#MACRO에 값이 있으면 그것이 우선이고,
        env `MACRO_LLM_DAILY_TOKEN_BUDGET`는 CONFIG 항목이 없을 때 초기값으로만 쓴다
        (운영자가 CONFIG를 조정했는데 다음 기동이 env로 되돌리는 문제 방지)."""
        stored = self.store.get_config_item() or {}
        if stored.get("llm_daily_token_budget") is not None:
            return int(stored["llm_daily_token_budget"])
        raw = os.environ.get("MACRO_LLM_DAILY_TOKEN_BUDGET")
        if raw:
            budget = int(raw)
            self.store.set_config(llm_daily_token_budget=budget)
            return budget
        return int(self.store.get_config()["llm_daily_token_budget"])

    def _seen_cb_urls(self) -> set[str]:
        seen: set[str] = set()
        for iso in self._isos:
            for d in self.store.list_docs("cb_stance", iso, limit=SEEN_CB_DOCS_PER_COUNTRY):
                if d.get("source_url"):
                    seen.add(str(d["source_url"]))
        return seen

    def _countries_for(self, name: str) -> list[Any]:
        """소스가 실제 담당하는 국가(지표별 유로 규칙·only·코드 자리표시자 반영) ∩ --countries."""
        inds = self.registry.indicators_for_source(name)
        if not inds:
            # 레지스트리에 지표가 없는 정성 소스(energy_policy 등): 선택 국가 전부 (모듈이 자체 판단)
            return [c for c in self.registry.countries if c.iso in self._isos]
        isos: set[str] = set()
        for ind in inds:
            isos.update(c.iso for c in self.registry.countries_for_source(name, ind.id))
        return [c for c in self.registry.countries if c.iso in isos and c.iso in self._isos]

    # ------------------------------------------------------------ 소스 실행
    def _cadence_check(self, name: str) -> tuple[bool, str]:
        if self.opts.force:
            return True, "force"
        latest = self.store.get_ingest_latest(name)
        if not latest:
            return True, "첫 실행"
        due = _parse_dt(latest.get("next_due"))
        if due is not None and due > datetime.now(timezone.utc):
            return False, f"next_due {latest['next_due']} 미도래 (마지막 {latest.get('finished_at')})"
        return True, f"next_due {latest.get('next_due') or '없음'} 도래"

    def _run_source(self, name: str) -> None:
        module = SOURCE_MODULES[name]
        run = SourceRun(name=name, cadence=str(getattr(module, "CADENCE", "daily")), started_at=now_iso())
        self.runs[name] = run
        assert self.ctx is not None
        ctx = self.ctx

        due, note = self._cadence_check(name)
        if not due:
            run.status, run.note, run.finished_at = "skipped", note, now_iso()
            logger.info("[%s] 건너뜀 — %s", name, note)
            return
        countries = self._countries_for(name)
        indicators = self.registry.indicators_for_source(name)
        run.targets = [c.iso for c in countries]
        if not countries:
            run.status, run.note, run.finished_at = "skipped", "대상 국가 없음", now_iso()
            logger.info("[%s] 건너뜀 — 대상 국가 없음", name)
            return
        if name == "companies":
            ctx.extra["fx_rates"] = self._fx_rates(countries)

        logger.info(
            "[%s] 수집 시작 (%s) — 국가 %d개, 지표 %s",
            name,
            note,
            len(countries),
            ",".join(i.id for i in indicators) or "(모듈 자체 판단)",
        )
        n_err0 = len(ctx.errors)
        t0 = time.monotonic()
        obs: list[Observation] = []
        docs: list[Doc] = []
        try:
            obs = list(module.collect(countries, indicators, ctx) or [])
            if hasattr(module, "collect_docs"):
                docs = list(module.collect_docs(countries, ctx) or [])
        except Exception as exc:  # noqa: BLE001 - 소스 단위 실패 격리
            logger.exception("[%s] 소스 실행 실패 — 다음 소스로 진행", name)
            run.errors.append(f"{name}:*: {type(exc).__name__}: {exc}")
            run.status = "failed"
        run.elapsed = time.monotonic() - t0
        run.errors.extend(ctx.errors[n_err0:])
        run.raw_obs, run.docs = obs, docs
        self.buckets[name] = obs
        if run.status != "failed":
            if run.errors and not obs and not docs:
                run.status = "failed"
            elif run.errors:
                run.status = "partial"
        if name == "energy_policy":
            self._record_energy_hashes(run, ctx)
        run.finished_at = now_iso()
        logger.info(
            "[%s] 완료 — 관측 %d건 · 문서 %d건 · 오류 %d건 · %.1f초 · 상태 %s",
            name,
            len(obs),
            len(docs),
            len(run.errors),
            run.elapsed,
            run.status,
        )

    def _fx_rates(self, countries: Sequence[Any]) -> dict[str, float]:
        """`{통화: 현지통화/USD}` — 이번 실행(bis M > yahoo D)의 최신값, 없으면 저장된 최신 M/D."""
        latest: dict[str, tuple[tuple[int, int, int], float]] = {}
        for module in ("bis", "yahoo"):
            for o in self.buckets.get(module, []):
                if o.indicator != "fx_usd" or not o.value:
                    continue
                key = aggregate.period_sort_key(o.freq, o.period[:10])
                cur = latest.get(o.iso)
                if cur is None or key > cur[0]:
                    latest[o.iso] = (key, float(o.value))
        rates: dict[str, float] = {"USD": 1.0}
        for c in countries:
            if c.iso == "US":
                continue
            hit = latest.get(c.iso)
            if hit is None:
                for freq in ("M", "D"):
                    rows = self.store.query_series("fx_usd", c.iso, freq, limit=1)
                    if rows and rows[-1].get("value"):
                        hit = ((0, 0, 0), float(rows[-1]["value"]))
                        break
            if hit is not None:
                rates.setdefault(c.ccy, hit[1])
        return rates

    def _record_energy_hashes(self, run: SourceRun, ctx: CollectContext) -> None:
        """{iso: 원문 sha1}을 갱신한다 — 모듈이 남긴 energy_hashes 우선, 없으면 문서 id 대조."""
        reported = ctx.extra.get("energy_hashes")
        found = False
        if isinstance(reported, dict):
            for iso, h in reported.items():
                if isinstance(iso, str) and isinstance(h, str) and h:
                    self._energy_hashes[iso.upper()] = h
                    found = True
        if not found:
            # 구버전 경로: 모듈이 조회한 해시(_ProbeSet)와 문서 id를 맞춰 역산
            probe = ctx.extra.get("seen_hashes")
            probed = list(getattr(probe, "probed", []))
            for doc in run.docs:
                for h in probed:
                    if _sha12(f"{doc.iso}:{h}") == doc.id:
                        self._energy_hashes[doc.iso] = h
                        break
        run.extra["hashes"] = dict(self._energy_hashes)

    # ------------------------------------------------------------ 병합·저장
    def _merge_and_put(self) -> None:
        ran = [n for n, r in self.runs.items() if r.ran]
        self.merged = merge_observations(self.registry, self.buckets, ran_sources=ran)
        self._prune_annual_fallbacks()
        self._fill_fx_monthly_fallback()
        # US 환율 기준값 합성은 월별 폴백 뒤 — yahoo D→M으로 채운 달도 기간 범위에 넣는다
        self._synthesize_us_fx()
        # LLM 문서 저장 (전문 → S3, 요약 → DOC#)
        for run in self.runs.values():
            if run.docs:
                run.n_docs = self._put_docs(run.docs)
        for name, obs in self.merged.items():
            run = self.runs.get(name)
            if not obs:
                continue
            stats = self.store.put_observations(obs)
            if run is not None:
                run.n_obs = len(obs)
                run.put = stats
            logger.info(
                "[%s] 저장 %d건 — 신규 %d · 갱신 %d · 동일 %d · 실패 %d (원 관측 %d건)",
                name,
                len(obs),
                stats["n_new"],
                stats["n_updated"],
                stats["n_unchanged"],
                stats["n_error"],
                len(self.buckets.get(name, [])),
            )

    def _prune_annual_fallbacks(self) -> None:
        """파생 지표(*_yoy)의 저빈도 최후 폴백(예: World Bank 연간 cpi_yoy·m2_yoy)을 정리한다.

        registry의 폴백 체인은 "월별 소스가 없는 국가에만" 연간 폴백을 쓴다. 그 (지표, 국가)에
        더 고빈도의 직접 관측(이번 실행 또는 저장)이 있거나 yoy_from 원천이 있어 파생이 가능하면
        연간 폴백을 버리고, 아니면 채택하되 `fallback_source`를 붙인다.
        """
        for module, obs in list(self.merged.items()):
            keep: list[Observation] = []
            dropped = 0
            for o in obs:
                if not self.registry.has_indicator(o.indicator):
                    keep.append(o)
                    continue
                ind = self.registry.indicator(o.indicator)
                if not ind.yoy_from or FREQ_RANK[o.freq] <= FREQ_RANK[ind.native_freq]:
                    keep.append(o)
                    continue
                if self._has_finer_path(ind, o.iso, o.freq):
                    dropped += 1
                    continue
                if "fallback_source" not in o.flags:
                    o.flags.append("fallback_source")
                keep.append(o)
            if dropped:
                logger.info("[%s] 연간 폴백 %d건 제외 — 더 고빈도 경로가 있는 (지표, 국가)", module, dropped)
            self.merged[module] = keep

    def _has_finer_path(self, ind: Indicator, iso: str, freq: str) -> bool:
        """(지표, 국가)에 freq보다 고빈도의 직접 관측 또는 yoy_from 원천 시계열이 있는가."""
        finer = [f for f in ind.store_freqs if FREQ_RANK[f] < FREQ_RANK[freq]]
        for f in finer:
            if self._has_direct(ind.id, iso, f):
                return True
        src = str(ind.yoy_from)
        src_freq = self.registry.indicator(src).native_freq if self.registry.has_indicator(src) else None
        if src_freq and FREQ_RANK[src_freq] < FREQ_RANK[freq]:
            if self._run_obs(src, iso, src_freq):
                return True
            if self.store.query_series(src, iso, src_freq, limit=1):
                return True
        return False

    def _synthesize_us_fx(self) -> None:
        """US fx_usd 월별(=1.0)을 합성한다 — 소스가 없어 히트맵 '미국·통화가치'가 비는 문제.

        조건: fx_usd가 이번 실행의 대상 지표이고 US가 대상 국가이며, 다른 국가의 fx_usd
        월별 관측이 실제로 있을 때만. 합성값은 `derived` 버킷에 담아 저장·파생 단계가
        이번 실행의 관측으로 함께 보게 한다(fx_value_index US = 100이 자연히 생성된다).
        """
        if US_FX_ISO not in self._isos:
            return
        others: list[Observation] = []
        for obs in self.merged.values():
            for o in obs:
                if o.indicator != "fx_usd":
                    continue
                if o.iso == US_FX_ISO:
                    # 소스가 US 환율을 냈으면(어느 빈도든) 합성하지 않는다 — 집계가 월별을 만든다
                    return
                others.append(o)
        synth = synth_us_fx_monthly(others)
        if not synth:
            return
        self.merged.setdefault(DERIVED_SOURCE, []).extend(synth)
        logger.info(
            "[%s] fx_usd US 기준값 합성 %d건 (%s~%s, value=1.0) — 기준통화라 소스가 없음",
            DERIVED_SOURCE,
            len(synth),
            synth[0].period,
            synth[-1].period,
        )

    def _fill_fx_monthly_fallback(self) -> None:
        """fx_usd 월별 특례: BIS가 못 낸 국가는 yahoo 일별을 D→M 기말 집계해 fallback으로 채운다."""
        daily_by_iso: dict[str, list[Observation]] = {}
        monthly_isos: set[str] = set()
        for module, obs in self.merged.items():
            for o in obs:
                if o.indicator != "fx_usd":
                    continue
                if o.freq == "M":
                    monthly_isos.add(o.iso)
                elif o.freq == "D" and module == "yahoo":
                    daily_by_iso.setdefault(o.iso, []).append(o)
        bis_ran = "bis" in self.buckets
        filled: list[Observation] = []
        for iso, daily in daily_by_iso.items():
            if iso in monthly_isos:
                continue
            if not bis_ran and self.store.query_series("fx_usd", iso, "M", limit=1):
                continue  # BIS 미참여 실행이고 저장된 월별이 있으면 덮어쓰지 않는다
            filled.extend(fill_fx_month_from_daily(daily, today=self.today))
        if filled:
            self.merged.setdefault("yahoo", []).extend(filled)
            logger.info(
                "[yahoo] fx_usd 월별 폴백 %d건 (%s) — BIS 월별 부재",
                len(filled),
                ",".join(sorted({o.iso for o in filled})),
            )

    def _put_docs(self, docs: list[Doc]) -> int:
        assert self.ctx is not None
        texts = self.ctx.extra.get("doc_texts") if isinstance(self.ctx.extra.get("doc_texts"), dict) else {}
        for doc in docs:
            if not doc.ai_generated:
                continue
            text = texts.get(f"{doc.type}/{doc.iso}/{doc.id}") or _doc_text(doc)
            try:
                self.store.save_doc_text(doc, text)
            except Exception as exc:  # noqa: BLE001 - 전문 저장 실패가 문서 저장을 막지 않게
                logger.warning("문서 전문 저장 실패 %s/%s/%s: %s", doc.type, doc.iso, doc.id, exc)
        return self.store.put_docs(docs)

    # ------------------------------------------------------------ 파생·집계
    def _derive(self) -> None:
        derived_all: list[Observation] = []
        for ind in self.registry.indicators:
            if not ind.is_derived:
                continue
            try:
                if ind.yoy_from:
                    derived_all.extend(self._derive_yoy(ind))
                elif ind.derived_from == "fx_usd":
                    derived_all.extend(self._derive_fx_value_index(ind))
                # fuel_dep_* 등 `derived via <source>` 는 소스 모듈이 이미 만들었다
            except Exception as exc:  # noqa: BLE001 - 지표 단위 실패 격리
                logger.exception("파생 실패 %s — 다른 지표는 계속", ind.id)
                self.post_errors.append(f"derive:{ind.id}: {type(exc).__name__}: {exc}")
        if derived_all:
            stats = self.store.put_observations(derived_all)
            self.derived_stats["derived"] = len(derived_all)
            logger.info(
                "파생 %d건 저장 — 신규 %d · 갱신 %d · 동일 %d",
                len(derived_all),
                stats["n_new"],
                stats["n_updated"],
                stats["n_unchanged"],
            )
        self._aggregate_low_freq(derived_all)

    def _isos_for(self, indicator_id: str) -> list[str]:
        """지표의 대상 국가 (유로 회원국은 공유 지표에서 제외)."""
        return [i for i in self._isos if not _is_euro_member_shared(self.registry, indicator_id, i)]

    def _run_obs(self, indicator: str, iso: str, freq: str) -> list[Observation]:
        """이번 실행에서 병합·채택된 (indicator, iso, freq) 관측."""
        return [
            o
            for obs in self.merged.values()
            for o in obs
            if o.indicator == indicator and o.iso == iso and o.freq == freq
        ]

    def _series_with_run(
        self, indicator: str, iso: str, freq: str, from_period: str | None = None
    ) -> list[Observation]:
        """저장된 시계열(from_period 이후) 위에 이번 실행 관측을 덮어 원천 시계열을 만든다."""
        by_period: dict[str, Observation] = {}
        for row in self.store.query_series(indicator, iso, freq, from_period=from_period):
            try:
                by_period[row["period"]] = obs_from_item(row)
            except (KeyError, ValueError):
                continue
        for o in self._run_obs(indicator, iso, freq):
            by_period[o.period] = o
        return sorted(by_period.values(), key=lambda o: aggregate.period_sort_key(o.freq, o.period))

    def _has_direct(self, indicator: str, iso: str, freq: str) -> bool:
        """(indicator, iso, freq)에 소스가 직접 준 관측(source != derived)이 있는가."""
        if any(o.source != DERIVED_SOURCE for o in self._run_obs(indicator, iso, freq)):
            return True
        rows = self.store.query_series(indicator, iso, freq, limit=1)
        return bool(rows) and rows[-1].get("source") != DERIVED_SOURCE

    def _incremental(self) -> bool:
        """증분 실행인가 (ctx.since 有). 전체 수집(since None)은 전 범위·전 국가를 다시 계산한다."""
        return bool(self.ctx is not None and self.ctx.since is not None)

    def _window_start(self, freq: str, lag: int) -> str | None:
        """증분 실행의 파생 조회 시작 기간 = since가 속한 기간 − lag. 전체 수집이면 None."""
        if not self._incremental():
            return None
        assert self.ctx is not None and self.ctx.since is not None
        return aggregate.previous_period(freq, _period_of(freq, self.ctx.since), lag)

    def _touched_isos(self, indicator: str, freq: str) -> set[str] | None:
        """증분 실행이면 이번 실행이 (indicator, freq)를 건드린 국가 집합, 전체 수집이면 None."""
        if not self._incremental():
            return None
        return {
            o.iso
            for obs in self.merged.values()
            for o in obs
            if o.indicator == indicator and o.freq == freq
        }

    def _derive_yoy(self, ind: Indicator) -> list[Observation]:
        src_id = str(ind.yoy_from)
        src = self.registry.indicator(src_id)
        freq = src.native_freq
        lag = aggregate.DEFAULT_YOY_LAG.get(freq, 12)
        start = self._window_start(freq, lag)
        touched = self._touched_isos(src_id, freq)
        out: list[Observation] = []
        for iso in self._isos_for(ind.id):
            if touched is not None and iso not in touched:
                continue  # 증분 실행: 원천이 바뀌지 않은 국가는 다시 계산하지 않는다
            if self._has_direct(ind.id, iso, freq):
                continue  # 예: OECD가 주는 cpi_yoy(GY) — 파생하지 않는다
            series = [
                o
                for o in self._series_with_run(src_id, iso, freq, from_period=start)
                if o.value is not None
            ]
            if not series:
                continue
            try:
                out.extend(aggregate.yoy(series, target_indicator=ind.id))
            except ValueError as exc:
                logger.warning("전년비 파생 실패 %s/%s: %s", ind.id, iso, exc)
        if out:
            logger.info("%s 파생 %d건 (%s ← %s)", ind.id, len(out), freq, src_id)
        return out

    def _derive_fx_value_index(self, ind: Indicator) -> list[Observation]:
        start = self._window_start("M", FX_VALUE_LAG_MONTHS)
        touched = self._touched_isos("fx_usd", "M")
        out: list[Observation] = []
        for iso in self._isos_for(ind.id):
            if touched is not None and iso not in touched:
                continue
            fx = [o for o in self._series_with_run("fx_usd", iso, "M", from_period=start) if o.value]
            out.extend(fx_value_index_yearly(fx))
        if out:
            logger.info("%s 파생 %d건 (fx_usd M, 1년 전 대비)", ind.id, len(out))
        return out

    def _aggregate_low_freq(self, derived: list[Observation]) -> None:
        """store_freqs의 저빈도(M/Q/Y)를 만든다 — 이번 실행이 건드린 (지표, 국가)의 영향 버킷만.

        - 소스(또는 fx 폴백)가 이번 실행에 직접 만든 빈도는 건드리지 않는다 (예: BIS fx_usd M).
        - agg=last는 target보다 고빈도 중 **가장 저빈도** 가용 시계열에서(M→Q, Q→Y: 기말값 동일,
          1차 소스의 월별을 존중), agg=mean/sum은 **가장 고빈도** 원천에서 직접 집계한다.
        - **같은 버킷(period)** 에 저장된 항목의 source가 집계 결과의 source와 다르면 그 버킷만 덮지
          않는다(예: 저장된 BIS 월별 환율을 yahoo 일별 집계로 대체하지 않음). 가장 최신 항목과
          비교하면 poll_of_polls가 만든 이번 달 `derived` M 때문에 지난달 W→M 집계가 영구 스킵된다.
        - 여론조사(W) 원천의 진행 중 버킷은 poll_of_polls 단계가 가중 평균으로 채우므로 제외.
        """
        touched: dict[tuple[str, str], dict[str, set[str]]] = {}
        for obs in [*self.merged.values(), derived]:
            for o in obs:
                touched.setdefault((o.indicator, o.iso), {}).setdefault(o.freq, set()).add(o.period)
        total = 0
        out: list[Observation] = []
        for (ind_id, iso), by_freq in touched.items():
            if not self.registry.has_indicator(ind_id):
                continue
            ind = self.registry.indicator(ind_id)
            if ind.agg not in ("last", "mean", "sum") or ind.is_composite:
                continue
            native_rank = FREQ_RANK.get(ind.native_freq, 0)
            for target in ind.store_freqs:
                if FREQ_RANK[target] <= native_rank or target in by_freq:
                    continue
                finer_touched = {f: ps for f, ps in by_freq.items() if FREQ_RANK[f] < FREQ_RANK[target]}
                if not finer_touched:
                    continue
                start_bucket = min(
                    (aggregate.bucket_of(f, p, target) for f, ps in finer_touched.items() for p in ps),
                    key=lambda b: aggregate.period_sort_key(target, b),
                )
                candidates = sorted(
                    (f for f in ind.store_freqs if FREQ_RANK[f] < FREQ_RANK[target]),
                    key=lambda f: FREQ_RANK[f],
                    reverse=(ind.agg == "last"),
                )
                series: list[Observation] = []
                src_freq = candidates[0] if candidates else ind.native_freq
                for src_freq in candidates:
                    start = _bucket_start(src_freq, target, start_bucket)
                    series = [
                        o
                        for o in self._series_with_run(ind_id, iso, src_freq, from_period=start)
                        if o.value is not None
                        and aggregate.period_sort_key(target, aggregate.bucket_of(src_freq, o.period, target))
                        >= aggregate.period_sort_key(target, start_bucket)
                    ]
                    if series:
                        break
                if not series:
                    continue
                try:
                    agg = aggregate.to_lower_freq(series, target, ind.agg, today=self.today)
                except ValueError as exc:
                    logger.warning("저빈도 집계 실패 %s/%s %s→%s: %s", ind_id, iso, src_freq, target, exc)
                    continue
                if ind.native_freq == "W":
                    # 진행 중인 달은 poll_of_polls 단계의 몫 (가중 평균 · source=derived)
                    agg = [o for o in agg if "partial_period" not in o.flags]
                if not agg:
                    continue
                stored_by_period = {
                    str(r.get("period")): r
                    for r in self.store.query_series(ind_id, iso, target, from_period=agg[0].period)
                    if r.get("period")
                }
                for o in agg:
                    prev = stored_by_period.get(o.period)
                    if prev is not None and prev.get("source") != o.source:
                        logger.debug(
                            "집계 건너뜀 %s/%s %s#%s — 저장된 소스 %s ≠ 원천 %s",
                            ind_id, iso, target, o.period, prev.get("source"), o.source,
                        )
                        continue
                    out.append(o)
        if out:
            stats = self.store.put_observations(out)
            total = len(out)
            logger.info(
                "저빈도 집계 %d건 저장 — 신규 %d · 갱신 %d · 동일 %d",
                total,
                stats["n_new"],
                stats["n_updated"],
                stats["n_unchanged"],
            )
        self.derived_stats["aggregated"] = total

    # ------------------------------------------------------------ 정성 후처리
    def _poll_of_polls(self) -> None:
        n_docs = 0
        obs: list[Observation] = []
        asof = self.today.isoformat()
        month = asof[:7]
        for iso in self._isos:
            items = self.store.list_docs("poll", iso, limit=POLL_DOC_LIMIT)
            if not items:
                continue
            polls: list[Doc] = []
            for it in items:
                try:
                    polls.append(doc_from_item(it))
                except (KeyError, ValueError):
                    continue
            ruling = self._ruling_party(iso)
            pop = aggregate.poll_of_polls(polls, asof=asof, ruling_party=ruling, window_days=POLL_WINDOW_DAYS)
            if not pop:
                continue
            country = self.registry.country(iso)
            election = self.store.latest_doc("election", iso) or {}
            source_url = str(election.get("source_url") or (polls[0].source_url if polls else ""))
            doc = Doc(
                type="poll_of_polls",
                iso=iso,
                date=asof,
                id=_sha12(f"poll_of_polls:{iso}:{asof}"),
                title_ko=f"{country.name_ko} 여론조사 평균 (최근 {POLL_WINDOW_DAYS}일)",
                summary_ko=_pop_summary(pop),
                source_url=source_url,
                source_name="자체 계산 (poll of polls, 표본·최근성 가중 평균)",
                payload=pop,
                ai_generated=False,
                confidence=1.0,
                review_status="approved",
            )
            n_docs += self.store.put_docs([doc])
            results = pop.get("results") or {}
            obs.append(
                Observation(
                    indicator="party_support",
                    iso=iso,
                    freq="M",
                    period=month,
                    value=pop.get("ruling_pct"),
                    unit="%",
                    source=DERIVED_SOURCE,
                    series_id=f"poll_of_polls@{iso}",
                    source_url=source_url,
                    method=(
                        f"최근 {POLL_WINDOW_DAYS}일 여론조사 {pop.get('n_polls')}건의 표본·최근성 가중 평균 "
                        "(value=집권당 지지율, payload.results=전 정당)"
                    ),
                    payload={
                        "results": results,
                        "ruling_party": ruling,
                        "n_polls": pop.get("n_polls"),
                        "asof": asof,
                    },
                    flags=["derived"],
                )
            )
            if pop.get("gov_approval") is not None:
                obs.append(
                    Observation(
                        indicator="gov_approval",
                        iso=iso,
                        freq="M",
                        period=month,
                        value=float(pop["gov_approval"]),
                        unit="%",
                        source=DERIVED_SOURCE,
                        series_id=f"poll_of_polls@{iso}",
                        source_url=source_url,
                        method=f"최근 {POLL_WINDOW_DAYS}일 여론조사의 정부 지지율 가중 평균",
                        flags=["derived"],
                    )
                )
        if obs:
            stats = self.store.put_observations(obs)
            logger.info(
                "poll_of_polls 문서 %d건 · 관측 %d건 (신규 %d · 갱신 %d)",
                n_docs,
                len(obs),
                stats["n_new"],
                stats["n_updated"],
            )
        self.derived_stats["poll_of_polls"] = n_docs

    def _ruling_party(self, iso: str) -> str | None:
        election = self.store.latest_doc("election", iso)
        if election and (election.get("payload") or {}).get("ruling_party"):
            return str(election["payload"]["ruling_party"])
        try:
            meta = polls_meta(iso)
        except Exception:  # noqa: BLE001 - 메타 파일 문제는 poll_of_polls를 막지 않는다
            return None
        ruling = (meta.get("election") or {}).get("ruling_party_ko") if isinstance(meta, dict) else None
        return str(ruling) if ruling else None

    # ------------------------------------------------------------ LATEST/SNAPSHOT
    def _rebuild(self) -> None:
        t0 = time.monotonic()
        all_isos = list(self.registry.iso_codes)
        self.latest_rows = []
        for ind in self.registry.indicators:
            isos = [i for i in all_isos if not _is_euro_member_shared(self.registry, ind.id, i)]
            pref = [f for f in DEFAULT_FREQ_PREF if f in ind.store_freqs] or list(DEFAULT_FREQ_PREF)
            rows = self.store.rebuild_latest(
                ind.id, isos, freq_pref=pref, rank=ind.unit not in NO_RANK_UNITS
            )
            self.latest_rows.extend(rows)
        self.n_latest = len(self.latest_rows)
        ids = list(self.registry.indicator_ids)
        for iso in all_isos:
            self.store.rebuild_snapshot(iso, ids)
        self.n_snapshot = len(all_isos)
        logger.info(
            "LATEST %d건 · SNAPSHOT %d건 재생성 (%.1f초)", self.n_latest, self.n_snapshot, time.monotonic() - t0
        )

    # ------------------------------------------------------------ weekly brief
    def _weekly_brief(self) -> None:
        run = SourceRun(name=BRIEF_SOURCE, cadence=str(getattr(weekly_brief, "CADENCE", "weekly")), started_at=now_iso())
        self.runs[BRIEF_SOURCE] = run
        due, note = self._cadence_check(BRIEF_SOURCE)
        if not due:
            run.status, run.note, run.finished_at = "skipped", note, now_iso()
            logger.info("[%s] 건너뜀 — %s", BRIEF_SOURCE, note)
            return
        if self.opts.no_llm:
            run.status, run.note, run.finished_at = "skipped", "no_llm", now_iso()
            logger.info("[%s] 건너뜀 — LLM 비활성", BRIEF_SOURCE)
            return
        assert self.ctx is not None
        ctx = self.ctx
        latest_rows = self.latest_rows or self._latest_rows_from_store()
        recent = self._recent_docs()
        n_err0 = len(ctx.errors)
        t0 = time.monotonic()
        try:
            doc = weekly_brief.build_brief(latest_rows, recent, ctx)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[%s] 실패", BRIEF_SOURCE)
            run.errors.append(f"{BRIEF_SOURCE}:G20: {type(exc).__name__}: {exc}")
            doc = None
        run.elapsed = time.monotonic() - t0
        run.errors.extend(ctx.errors[n_err0:])
        if doc is not None:
            run.docs = [doc]
            run.n_docs = self._put_docs([doc])
            run.status = "partial" if run.errors else "ok"
        else:
            run.status = "failed" if run.errors else "ok"
            run.note = "브리프 없음"
        run.finished_at = now_iso()
        logger.info("[%s] 완료 — 문서 %d건 · 오류 %d건 · 상태 %s", BRIEF_SOURCE, run.n_docs, len(run.errors), run.status)

    def _latest_rows_from_store(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for ind in self.registry.indicators:
            rows.extend(self.store.get_latest(ind.id))
        return rows

    def _recent_docs(self) -> list[dict[str, Any]]:
        cutoff = (self.today - timedelta(days=BRIEF_RECENT_DAYS)).isoformat()
        out: list[dict[str, Any]] = []
        for iso in self._isos:
            for t in BRIEF_DOC_TYPES:
                for d in self.store.list_docs(t, iso, limit=5):
                    if str(d.get("date", "")) >= cutoff:
                        out.append(d)
        return out

    # ------------------------------------------------------------ INGEST/요약
    def _log_ingest(self) -> None:
        for name, run in self.runs.items():
            if not run.ran:
                continue
            failed = run.failed_isos()
            summary: dict[str, Any] = {
                "status": run.status,
                "started_at": run.started_at,
                "finished_at": run.finished_at or now_iso(),
                "n_obs": run.n_obs,
                "n_obs_raw": len(run.raw_obs),
                "n_new": run.put.get("n_new", 0),
                "n_updated": run.put.get("n_updated", 0),
                "n_docs": run.n_docs,
                "errors": run.errors[:_MAX_ERRORS_LOGGED],
                "n_errors": len(run.errors),
                "countries_ok": [i for i in run.targets if i not in failed],
                "countries_failed": failed,
                "cadence": run.cadence,
                "cadence_days": CADENCE_DAYS.get(run.cadence, 1),
                "elapsed_sec": round(run.elapsed, 1),
                "dry_run": self.opts.dry_run,
                "since": self.ctx.since.isoformat() if self.ctx and self.ctx.since else None,
                "post_errors": list(self.post_errors),
                **run.extra,
            }
            try:
                self.store.log_ingest(name, summary)
            except Exception as exc:  # noqa: BLE001 - 한 소스의 기록 실패가 다른 소스 기록을 막지 않게
                logger.exception("INGEST 기록 실패 %s", name)
                self.post_errors.append(f"log_ingest:{name}: {type(exc).__name__}: {exc}")

    def _summary(self, elapsed: float) -> None:
        lines = [
            "",
            f"{'source':<14}{'status':<9}{'raw':>7}{'saved':>7}{'new':>6}{'upd':>6}{'docs':>6}{'err':>5}{'sec':>7}  note",
        ]
        for name, r in self.runs.items():
            lines.append(
                f"{name:<14}{r.status:<9}{len(r.raw_obs):>7}{r.n_obs:>7}{r.put.get('n_new', 0):>6}"
                f"{r.put.get('n_updated', 0):>6}{r.n_docs:>6}{len(r.errors):>5}{r.elapsed:>7.1f}  {r.note}"
            )
        lines.append(
            f"파생 {self.derived_stats.get('derived', 0)}건 · 저빈도 집계 {self.derived_stats.get('aggregated', 0)}건 · "
            f"poll_of_polls {self.derived_stats.get('poll_of_polls', 0)}건 · LATEST {self.n_latest}건 · "
            f"SNAPSHOT {self.n_snapshot}건 · 총 {elapsed:.1f}초"
        )
        logger.info("\n".join(lines))


# ------------------------------------------------------------------ 헬퍼
def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _sha12(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", "replace")).hexdigest()[:12]


def _period_of(freq: str, d: date) -> str:
    """날짜가 속한 기간 문자열 (D/W/E `YYYY-MM-DD`, M `YYYY-MM`, Q `YYYY-Qn`, Y `YYYY`)."""
    if freq == "M":
        return f"{d.year:04d}-{d.month:02d}"
    if freq == "Q":
        return f"{d.year:04d}-Q{(d.month - 1) // 3 + 1}"
    if freq == "Y":
        return f"{d.year:04d}"
    return d.isoformat()


def _bucket_start(src_freq: str, target_freq: str, bucket: str) -> str:
    """target 버킷(예: `2026-Q3`)의 첫 원천 기간(예: src M → `2026-07`, src D → `2026-07-01`)."""
    if target_freq == "Y":
        year, month = int(bucket), 1
    elif target_freq == "Q":
        y, q = bucket.split("-Q")
        year, month = int(y), (int(q) - 1) * 3 + 1
    else:  # M
        y, m = bucket.split("-")
        year, month = int(y), int(m)
    if src_freq == "M":
        return f"{year:04d}-{month:02d}"
    if src_freq == "Q":
        return f"{year:04d}-Q{(month - 1) // 3 + 1}"
    return f"{year:04d}-{month:02d}-01"


def _doc_text(doc: Doc) -> str:
    """모듈이 원문을 남기지 않았을 때의 전문 대체: 제목·요약·인용·payload."""
    lines = [f"# {doc.title_ko}", "", f"- 출처: {doc.source_url}", f"- 날짜: {doc.date}", ""]
    if doc.summary_ko:
        lines += ["## 요약", "", doc.summary_ko, ""]
    if doc.quotes:
        lines += ["## 인용", ""] + [f"> {q}" for q in doc.quotes] + [""]
    lines += ["## payload", "", "```json", json.dumps(doc.payload, ensure_ascii=False, indent=2), "```"]
    return "\n".join(lines)


def _pop_summary(pop: dict[str, Any]) -> str:
    parts = [f"최근 {pop.get('window_days')}일 조사 {pop.get('n_polls')}건 가중 평균."]
    if pop.get("leader_party"):
        parts.append(f"1위 {pop['leader_party']} {pop.get('leader_pct')}%.")
    if pop.get("ruling_party") and pop.get("ruling_pct") is not None:
        parts.append(f"집권당 {pop['ruling_party']} {pop['ruling_pct']}%.")
    if pop.get("gov_approval") is not None:
        parts.append(f"정부 지지율 {pop['gov_approval']}%.")
    return " ".join(parts)


# ------------------------------------------------------------------ CLI
def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="G20 매크로 수집기 (CONTRACT 12장)")
    p.add_argument("--sources", default="", help="쉼표 구분 소스 (기본: 전부, CONTRACT 12장 순서)")
    p.add_argument("--countries", default="", help="쉼표 구분 ISO2 (기본: 레지스트리 20개국)")
    p.add_argument("--since", default=None, help="증분 시작일 YYYY-MM-DD (기본: 마지막 실행 − 45일)")
    p.add_argument("--dry-run", action="store_true", help="인메모리 테이블·S3, 원본은 ./.macro_raw/")
    p.add_argument("--dump", default=None, help="dry-run 결과를 JSON으로 저장 (devserver --load)")
    p.add_argument("--load", default=None, help="dry-run 시 기존 덤프 위에 이어서 수집")
    llm = p.add_mutually_exclusive_group()
    llm.add_argument("--no-llm", action="store_true", help="LLM 판정 생략 (dry-run 기본)")
    llm.add_argument("--llm", action="store_true", help="dry-run에서도 LLM 사용 (Bedrock 비용 발생)")
    p.add_argument("--force", action="store_true", help="케이던스(next_due) 무시")
    p.add_argument("--skip-derived", action="store_true", help="파생·저빈도 집계 생략")
    p.add_argument("--skip-rebuild", action="store_true", help="LATEST/SNAPSHOT 재생성 생략")
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


def options_from_args(args: argparse.Namespace) -> Options:
    no_llm = bool(args.no_llm) or (bool(args.dry_run) and not bool(args.llm))
    return Options(
        sources=[s.strip() for s in args.sources.split(",") if s.strip()] or list(DEFAULT_SOURCES),
        countries=[c.strip() for c in args.countries.split(",") if c.strip()],
        since=date.fromisoformat(args.since) if args.since else None,
        dry_run=bool(args.dry_run),
        no_llm=no_llm,
        force=bool(args.force),
        skip_derived=bool(args.skip_derived),
        skip_rebuild=bool(args.skip_rebuild),
        dump=args.dump,
        load=args.load,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # yfinance 등 타임아웃 없는 HTTP가 배치를 멈추지 않게 (webui/catalog/build_catalog.py와 동일)
    socket.setdefaulttimeout(120)
    opts = options_from_args(args)
    try:
        store, raw_saver = build_store(opts)
        collector = Collector(opts, store=store, raw_saver=raw_saver)
        return collector.run()
    except (ValueError, KeyError) as exc:
        logger.error("설정 오류: %s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
