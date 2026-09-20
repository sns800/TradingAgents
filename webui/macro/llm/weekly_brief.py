# ============================================================
# [모듈 개요] 주간 매크로 브리프 문서 (iso="G20", type=weekly_brief)
#
# CONTRACT.md 5장 weekly_brief payload = {week_start, bullets, evidence}.
# 수집기가 LATEST 재생성 후에 호출한다:
#   doc = weekly_brief.build_brief(latest_rows, recent_docs, ctx)
#   if doc: store.put_docs([doc])
#
# 입력(둘 다 dict 리스트, 키가 없어도 죽지 않게 관대하게 읽는다):
#   latest_rows : [{iso, indicator, value, unit, period, change, change_pct, rank}]
#   recent_docs : [{type, iso, date, id, title_ko, summary_ko, source_url}]
#
# 환각 방지 장치:
#   - evidence는 LLM이 낸 것 중 **입력으로 준 doc_key/url과 일치하는 것만** 남긴다
#   - quotes는 LLM이 아니라 근거 문서의 summary_ko 첫 문장에서 코드가 만든다
#     (schema가 ai_generated 문서에 quotes 1개 이상을 요구하므로)
# ============================================================
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from macro.schema import Doc, today_str

from .bedrock_json import json_schema_brief
from .common import llm_of, log, record_error
from .textutil import normalize_ws, sha12

SOURCE_NAME = "weekly_brief"
CADENCE = "weekly"
MAX_ROWS = 220
MAX_DOCS = 40
MAX_BULLETS = 8
MIN_BULLETS = 5

_SYSTEM_PROMPT = """당신은 G20 거시경제 주간 브리프 작성자입니다. 주어진 최신 지표 표와
최근 문서 요약만 근거로 한국어 불릿을 작성합니다. 입력에 없는 수치·사건은 절대 쓰지 마십시오.

- bullets: 5~8개. 각 1~2문장, 한국어. 숫자를 인용할 때는 입력 표의 값·기간을 그대로 씁니다.
  국가 간 정당 지지율 직접 비교는 하지 않습니다(조사 방식이 달라 비교 불가).
  통화·정책금리·물가·정치 이벤트·에너지 중 변화가 큰 것부터 씁니다.
- evidence: 각 불릿의 근거가 된 문서를 [문서 목록]에 적힌 doc_key와 url을 **그대로 복사**해
  나열합니다. 목록에 없는 doc_key나 url을 만들면 안 됩니다.
- confidence: 0~1."""


def build_brief(
    latest_rows: list[dict[str, Any]], recent_docs: list[dict[str, Any]], ctx: Any
) -> Doc | None:
    """주간 브리프 문서를 만든다. no_llm이거나 입력이 비면 None."""
    llm = llm_of(ctx)
    rows = [r for r in (latest_rows or []) if isinstance(r, dict)][:MAX_ROWS]
    docs = [d for d in (recent_docs or []) if isinstance(d, dict)][:MAX_DOCS]
    if llm is None:
        log(ctx, f"[{SOURCE_NAME}] no_llm 또는 LLM 미주입 → 브리프 생략")
        return None
    if not rows and not docs:
        log(ctx, f"[{SOURCE_NAME}] 입력이 비어 브리프 생략")
        return None

    allowed = _doc_index(docs)
    user = _compose_user(rows, docs)
    try:
        raw = llm.invoke_json(
            system=_SYSTEM_PROMPT,
            user=user,
            schema=json_schema_brief,
            tool_name="emit_brief",
            max_tokens=2500,
        )
    except Exception as exc:  # noqa: BLE001 - 예산 초과/스키마 실패 격리
        record_error(ctx, SOURCE_NAME, "G20", f"LLM 브리프 실패: {exc}")
        return None

    bullets: list[str] = []
    for b in raw.get("bullets") or []:
        s = normalize_ws(str(b or ""))
        if s and s not in bullets:
            bullets.append(s[:400])
        if len(bullets) >= MAX_BULLETS:
            break
    if len(bullets) < MIN_BULLETS:
        record_error(
            ctx, SOURCE_NAME, "G20", f"불릿이 {len(bullets)}개뿐이어서 브리프 폐기(최소 {MIN_BULLETS})"
        )
        return None

    evidence: list[dict[str, str]] = []
    for e in raw.get("evidence") or []:
        if not isinstance(e, dict):
            continue
        key = normalize_ws(str(e.get("doc_key") or ""))
        url = normalize_ws(str(e.get("url") or ""))
        hit = allowed.get(key)
        if hit is None or (url and url != hit["url"]):
            continue  # 입력에 없던 근거는 버린다 (환각 방지)
        item = {"doc_key": key, "url": hit["url"]}
        if item not in evidence:
            evidence.append(item)

    quotes = _quotes_from_docs(docs, evidence, allowed)
    if not quotes:
        record_error(ctx, SOURCE_NAME, "G20", "근거 문서 요약이 없어 브리프 폐기(quotes 필수)")
        return None

    week_start = _week_start()
    date_str = today_str()
    doc_id = sha12(f"weekly_brief:{week_start}")
    confidence = raw.get("confidence")
    try:
        conf = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        conf = 0.6
    return Doc(
        type="weekly_brief",
        iso="G20",
        date=date_str,
        id=doc_id,
        title_ko=normalize_ws(str(raw.get("title_ko") or ""))[:120]
        or f"G20 매크로 주간 브리프 ({week_start} 주)",
        summary_ko=" ".join(bullets[:2]),
        source_url="/#/macro",
        source_name="G20 매크로 대시보드 (자체 집계)",
        payload={"week_start": week_start, "bullets": bullets, "evidence": evidence},
        ai_generated=True,
        model_id=getattr(llm, "model_id", None),
        confidence=conf,
        quotes=quotes,
        s3_key=f"macro/docs/weekly_brief/G20/{date_str}_{doc_id}.md",
    )


# ---------------------------------------------------------------- 내부
def _week_start(today: Any = None) -> str:
    d = today or datetime.now(timezone.utc).date()
    return (d - timedelta(days=d.weekday())).isoformat()


def _doc_key(d: dict[str, Any]) -> str:
    """CONTRACT 4장 키 형식 그대로: 'DOC#<type>#<iso>|<date>#<id>'."""
    return f"DOC#{d.get('type')}#{str(d.get('iso') or '').upper()}|{d.get('date')}#{d.get('id')}"


def _doc_index(docs: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for d in docs:
        out[_doc_key(d)] = {
            "url": normalize_ws(str(d.get("source_url") or "")),
            "summary": normalize_ws(str(d.get("summary_ko") or "")),
            "title": normalize_ws(str(d.get("title_ko") or "")),
        }
    return out


def _first_sentence(text: str) -> str:
    t = normalize_ws(text)
    if not t:
        return ""
    for sep in ("。", ". ", "다. ", "! ", "? "):
        idx = t.find(sep)
        if idx > 10:
            return t[: idx + len(sep)].strip()
    return t[:200]


def _quotes_from_docs(
    docs: list[dict[str, Any]],
    evidence: list[dict[str, str]],
    allowed: dict[str, dict[str, str]],
) -> list[str]:
    """근거 문서(없으면 입력 문서 앞쪽)의 summary 첫 문장을 quotes로 쓴다."""
    keys = [e["doc_key"] for e in evidence] or [_doc_key(d) for d in docs[:3]]
    out: list[str] = []
    for key in keys:
        info = allowed.get(key) or {}
        s = _first_sentence(info.get("summary") or info.get("title") or "")
        if s and s not in out:
            out.append(s)
        if len(out) >= 3:
            break
    return out


def _compose_user(rows: list[dict[str, Any]], docs: list[dict[str, Any]]) -> str:
    """입력을 LLM이 읽기 쉬운 압축 텍스트로 바꾼다 (토큰 절약)."""
    lines = ["[최신 지표] iso | 지표 | 값 | 단위 | 기간 | 변화"]
    for r in rows:
        lines.append(
            " | ".join(
                [
                    str(r.get("iso") or ""),
                    str(r.get("indicator") or ""),
                    _num(r.get("value")),
                    str(r.get("unit") or ""),
                    str(r.get("period") or ""),
                    _num(r.get("change") if r.get("change") is not None else r.get("change_pct")),
                ]
            )
        )
    lines.append("")
    lines.append("[문서 목록] doc_key | url | 제목 | 요약")
    for d in docs:
        lines.append(
            " | ".join(
                [
                    _doc_key(d),
                    normalize_ws(str(d.get("source_url") or "")),
                    normalize_ws(str(d.get("title_ko") or "")),
                    normalize_ws(str(d.get("summary_ko") or ""))[:300],
                ]
            )
        )
    return "\n".join(lines)


def _num(v: Any) -> str:
    if v is None:
        return ""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return normalize_ws(str(v))
    return f"{f:g}"
