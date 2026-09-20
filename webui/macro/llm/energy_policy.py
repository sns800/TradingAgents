# ============================================================
# [모듈 개요] 에너지 정책 원문 → energy_policy 문서 (LLM, 변경 감지)
#
# CONTRACT.md 5장 energy_policy payload = {targets, recent_changes, sources}.
# 케이던스 weekly + **원문 해시 변경 감지**: sources.yaml의 URL들을 모아 만든
# 평문의 sha1이 ctx.extra["seen_hashes"]에 이미 있으면 LLM을 호출하지 않는다
# (제안서 3.3 "LLM 호출은 변경 감지 시에만").
#
# 수집기(collect.py) 호출 규약:
#   docs = energy_policy.collect_docs(countries, ctx)          -> store.put_docs
#   obs  = energy_policy.collect(countries, indicators, ctx)   -> 항상 [] (수치 없음)
# ctx.extra["seen_hashes"]: set[str] — 이미 문서화한 원문 sha1(40자 hex) 전체 (입력).
# ctx.extra["energy_hashes"]: dict[iso, sha1] — 이번 실행에서 문서를 만든 원문 해시(출력).
#   수집기가 INGEST#energy_policy/LATEST.hashes에 저장해 다음 실행의 seen_hashes로 복원한다.
#   문서 payload에도 `content_sha1`을 넣어 문서만 봐도 어떤 원문인지 알 수 있게 한다.
# ctx.extra["doc_texts"]["energy_policy/<iso>/<id>"]: 이어붙인 원문 평문(출력) — 수집기가 S3 전문으로 저장.
# ============================================================
from __future__ import annotations

import hashlib
from typing import Any

from macro.schema import Doc, Observation, today_str

from .bedrock_json import json_schema_energy
from .common import (
    country_iso,
    country_name_ko,
    extra,
    fetch_text,
    keep_valid_quotes,
    llm_of,
    log,
    record_doc_text,
    record_error,
    save_raw,
)
from .meta import load_sources
from .textutil import html_to_text, normalize_ws, sha12

SOURCE_NAME = "energy_policy"
CADENCE = "weekly"
MAX_CHARS_PER_SOURCE = 6_000

_SYSTEM_PROMPT = """당신은 각국 에너지 정책 문서를 정리하는 분석가입니다.
주어진 원문(여러 URL의 본문을 이어붙인 것)에 실제로 적혀 있는 내용만 사용하고,
없는 수치·연도는 절대 만들지 마십시오.

- targets: 정량 목표를 한국어 한 문장씩. 가능한 한 **수치와 연도**를 포함합니다.
  (예: "2030년까지 재생에너지 발전 비중 32.9% 달성") 최대 6개.
- recent_changes: 최근 정책 변경·발표를 한국어 한 문장씩. 최대 5개. 없으면 빈 배열.
- title_ko: 한국어 제목 40자 이내.
- summary_ko: 한국어 요약 3~5문장.
- quotes: 목표·변경의 근거가 되는 **원문 문장을 원어 그대로** 1~3개. 번역·요약 금지.
- confidence: 0~1. 원문이 개요 수준이거나 목표가 불명확하면 낮게."""


def collect_docs(countries: list[Any], ctx: Any) -> list[Doc]:
    """국가별 에너지 정책 문서. 원문이 바뀌지 않았으면 아무것도 만들지 않는다."""
    meta_all = load_sources().get("energy_policy", {})
    seen_hashes = extra(ctx, "seen_hashes", set())
    llm = llm_of(ctx)
    docs: list[Doc] = []
    for country in countries:
        iso = country_iso(country)
        urls = [u for u in ((meta_all.get(iso) or {}).get("sources") or []) if u]
        if not urls:
            continue
        try:
            doc = _collect_country(country, iso, urls, ctx, llm, seen_hashes)
        except Exception as exc:  # noqa: BLE001 - 국가 단위 실패 격리
            record_error(ctx, SOURCE_NAME, iso, f"수집 실패: {exc}")
            continue
        if doc is not None:
            docs.append(doc)
    return docs


def collect(countries: list[Any], indicators: list[Any], ctx: Any) -> list[Observation]:
    """에너지 정책은 수치 관측치를 만들지 않는다 (문서 전용 소스)."""
    return []


def _collect_country(
    country: Any, iso: str, urls: list[str], ctx: Any, llm: Any, seen_hashes: Any
) -> Doc | None:
    name_ko = country_name_ko(country)
    chunks: list[str] = []
    used: list[str] = []
    for url in urls:
        html = fetch_text(ctx, SOURCE_NAME, iso, url)
        if html is None:
            continue
        text = html_to_text(html, max_chars=MAX_CHARS_PER_SOURCE)
        if len(text) < 200:
            record_error(ctx, SOURCE_NAME, iso, f"본문이 너무 짧음({len(text)}자): {url}")
            continue
        chunks.append(f"### 출처: {url}\n{text}")
        used.append(url)
    if not chunks:
        return None

    combined = "\n\n".join(chunks)
    content_hash = hashlib.sha1(combined.encode("utf-8", "replace")).hexdigest()
    if content_hash in seen_hashes:
        log(ctx, f"[{SOURCE_NAME}] {iso} 원문 변경 없음(sha1 {content_hash[:12]}) → 건너뜀")
        return None
    save_raw(ctx, SOURCE_NAME, f"{iso}_{content_hash[:12]}", combined)

    if llm is None:
        log(ctx, f"[{SOURCE_NAME}] {iso} no_llm: 원문만 저장")
        return None

    try:
        raw = llm.invoke_json(
            system=_SYSTEM_PROMPT,
            user=f"[국가] {name_ko} ({iso})\n[출처 수] {len(used)}\n\n{combined}",
            schema=json_schema_energy,
            tool_name="emit_energy",
            max_tokens=2000,
        )
    except Exception as exc:  # noqa: BLE001 - 예산 초과/스키마 실패 격리
        record_error(ctx, SOURCE_NAME, iso, f"LLM 정리 실패: {exc}")
        return None

    quotes = keep_valid_quotes(raw.get("quotes"), combined, max_n=3)
    if not quotes:
        record_error(ctx, SOURCE_NAME, iso, "인용문이 원문에 없어 문서 폐기")
        return None

    targets = _str_list(raw.get("targets"), limit=6)
    changes = _str_list(raw.get("recent_changes"), limit=5)
    if not targets and not changes:
        record_error(ctx, SOURCE_NAME, iso, "targets/recent_changes가 모두 비어 문서 폐기")
        return None

    date_str = today_str()
    doc_id = sha12(f"{iso}:{content_hash}")
    confidence = raw.get("confidence")
    try:
        conf = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        conf = 0.5
    doc = Doc(
        type="energy_policy",
        iso=iso,
        date=date_str,
        id=doc_id,
        title_ko=normalize_ws(str(raw.get("title_ko") or ""))[:120]
        or f"{name_ko} 에너지 정책 목표",
        summary_ko=normalize_ws(str(raw.get("summary_ko") or "")),
        source_url=used[0],
        source_name="IEA·정부 에너지 정책 문서",
        payload={
            "targets": targets,
            "recent_changes": changes,
            "sources": used,
            "content_sha1": content_hash,
        },
        ai_generated=True,
        model_id=getattr(llm, "model_id", None),
        confidence=conf,
        quotes=quotes,
        s3_key=f"macro/docs/energy_policy/{iso}/{date_str}_{doc_id}.md",
    )
    # 수집기가 INGEST에 저장할 원문 해시와 S3에 올릴 전문을 ctx.extra로 돌려준다
    box = getattr(ctx, "extra", None)
    if isinstance(box, dict):
        box.setdefault("energy_hashes", {})[iso] = content_hash
    record_doc_text(ctx, doc, combined)
    return doc


def _str_list(value: Any, *, limit: int) -> list[str]:
    out: list[str] = []
    if not isinstance(value, (list, tuple)):
        return out
    for item in value:
        s = normalize_ws(str(item or ""))
        if s and s not in out:
            out.append(s[:300])
        if len(out) >= limit:
            break
    return out
