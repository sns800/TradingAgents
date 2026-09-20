# ============================================================
# [모듈 개요] 정성 소스 공통 헬퍼 (CollectContext 안전 접근 + 인용 검증)
#
# sources/base.py(CollectContext)는 정량 소스 담당이 소유하므로, 이 계층은
# 아래 덕 타이핑 계약만 가정하고 속성이 없어도 죽지 않게 감싼다:
#   ctx.get(url, params=None, headers=None, timeout=30) -> requests.Response
#   ctx.save_raw(source, name, data) / ctx.record_error(source, iso, msg)
#   ctx.since · ctx.dry_run · ctx.no_llm · ctx.llm · ctx.extra: dict · ctx.log
# 국가 객체도 duck typing: country.iso · country.name_ko · country.euro
# ============================================================
from __future__ import annotations

import contextlib
from typing import Any

from .textutil import normalize_ws, quote_in_text

__all__ = [
    "BROWSER_HEADERS",
    "DOC_TEXT_MAX_CHARS",
    "HTTP_HEADERS",
    "clamp_half",
    "country_iso",
    "country_name_ko",
    "extra",
    "fetch_text",
    "headers_for",
    "is_euro",
    "keep_valid_quotes",
    "llm_of",
    "log",
    "record_error",
    "record_doc_text",
    "save_raw",
]

# 문서 전문(S3 macro/docs/...)으로 남기는 원문 상한. LLM 입력 상한과 같은 값이라
# 상한에 걸린 본문은 절단 표기를 붙여 저장한다.
DOC_TEXT_MAX_CHARS = 12_000

# 위키피디아·중앙은행 사이트는 빈 User-Agent를 차단한다 (위키는 정책상 필수)
HTTP_HEADERS = {
    "User-Agent": (
        "TradingAgentsMacroBot/1.0 (+https://github.com/TauricResearch/TradingAgents) "
        "python-requests"
    ),
    "Accept-Language": "en,ko;q=0.8",
}
# 일부 사이트는 봇 UA를 WAF로 차단한다 (실측: Bank Indonesia는 연결 리셋, SAMA는
# F5/TSPD 챌린지). sources.yaml의 `user_agent: browser`로 이 헤더를 쓰게 한다.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en,ko;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
}


def headers_for(user_agent: str | None) -> dict[str, str]:
    """sources.yaml의 user_agent 설정을 요청 헤더로 바꾼다."""
    if not user_agent:
        return dict(HTTP_HEADERS)
    if str(user_agent).strip().lower() == "browser":
        return dict(BROWSER_HEADERS)
    out = dict(HTTP_HEADERS)
    out["User-Agent"] = str(user_agent)
    return out


def country_iso(country: Any) -> str:
    return str(getattr(country, "iso", "") or "").upper()


def country_name_ko(country: Any) -> str:
    return str(getattr(country, "name_ko", "") or country_iso(country))


def is_euro(country: Any) -> bool:
    """유로 회원국 여부. registry는 yes/no 문자열 또는 bool을 쓸 수 있다."""
    v = getattr(country, "euro", False)
    if isinstance(v, str):
        return v.strip().lower() in {"yes", "true", "y", "1"}
    return bool(v)


def log(ctx: Any, msg: str) -> None:
    fn = getattr(ctx, "log", None)
    if callable(fn):
        # 로깅 실패로 수집을 멈추지 않는다
        with contextlib.suppress(Exception):
            fn(msg)


def record_error(ctx: Any, source: str, iso: str, msg: str) -> None:
    fn = getattr(ctx, "record_error", None)
    if callable(fn):
        try:
            fn(source, iso, msg)
            return
        except Exception:  # noqa: BLE001
            pass
    errors = getattr(ctx, "errors", None)
    if isinstance(errors, list):
        errors.append(f"{source}:{iso}: {msg}")


def save_raw(ctx: Any, source: str, name: str, data: bytes | str) -> None:
    fn = getattr(ctx, "save_raw", None)
    if not callable(fn):
        return
    try:
        fn(source, name, data)
    except Exception as exc:  # noqa: BLE001 - 원본 보존 실패는 경고만
        log(ctx, f"[{source}] 원본 저장 실패 {name}: {exc}")


def record_doc_text(ctx: Any, doc: Any, text: str) -> None:
    """문서 원문을 `ctx.extra["doc_texts"]["<type>/<iso>/<id>"]`에 남긴다.

    collect.py의 `_put_docs`가 이 키를 읽어 `store.save_doc_text`로 S3에 올린다
    (없으면 제목·요약·payload로 만든 대체 본문을 쓴다). 상한을 넘으면 앞에서 자르고
    끝에 절단 표기를 붙인다 — 잘린 사실을 모른 채 인용을 검증하는 실수를 막는다.
    """
    box = getattr(ctx, "extra", None)
    if not isinstance(box, dict):
        return
    body = text or ""
    if len(body) > DOC_TEXT_MAX_CHARS:
        body = body[:DOC_TEXT_MAX_CHARS].rstrip()
    if len(text or "") >= DOC_TEXT_MAX_CHARS:
        body += f"\n\n… ({DOC_TEXT_MAX_CHARS:,}자에서 절단)"
    key = f"{getattr(doc, 'type', '')}/{getattr(doc, 'iso', '')}/{getattr(doc, 'id', '')}"
    box.setdefault("doc_texts", {})[key] = body


def extra(ctx: Any, key: str, default: Any) -> Any:
    """ctx.extra에서 값을 꺼낸다 (없으면 default). 수집기가 채워주는 힌트용."""
    box = getattr(ctx, "extra", None)
    if isinstance(box, dict):
        v = box.get(key, default)
        return default if v is None else v
    return default


def llm_of(ctx: Any) -> Any | None:
    """LLM 사용 가능하면 클라이언트를, --no-llm이거나 미주입이면 None."""
    if bool(getattr(ctx, "no_llm", False)):
        return None
    return getattr(ctx, "llm", None)


def fetch_text(
    ctx: Any,
    source: str,
    iso: str,
    url: str,
    *,
    timeout: int = 30,
    user_agent: str | None = None,
) -> str | None:
    """ctx.get으로 HTML을 받아 문자열로 반환. 실패는 None + record_error."""
    getter = getattr(ctx, "get", None)
    if not callable(getter):
        record_error(ctx, source, iso, "ctx.get이 없어 원문을 받을 수 없음")
        return None
    try:
        resp = getter(url, headers=headers_for(user_agent), timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - 국가 단위 실패 격리
        record_error(ctx, source, iso, f"요청 실패 {url}: {exc}")
        return None
    status = getattr(resp, "status_code", 200)
    if status and int(status) >= 400:
        record_error(ctx, source, iso, f"HTTP {status} {url}")
        return None
    text = getattr(resp, "text", None)
    if text is None:
        content = getattr(resp, "content", b"") or b""
        text = content.decode("utf-8", "replace")
    if not text:
        record_error(ctx, source, iso, f"빈 응답 {url}")
        return None
    return text


def keep_valid_quotes(quotes: Any, text: str, *, max_n: int = 3) -> list[str]:
    """LLM 인용 중 원문에 실제로 존재하는 것만 남긴다 (중복 제거, 최대 max_n개)."""
    out: list[str] = []
    if not isinstance(quotes, (list, tuple)):
        return out
    for q in quotes:
        if not isinstance(q, str):
            continue
        cleaned = normalize_ws(q).strip("\"'“”‘’ ")
        if not cleaned or cleaned in out:
            continue
        if quote_in_text(cleaned, text):
            out.append(cleaned)
        if len(out) >= max_n:
            break
    return out


def clamp_half(value: Any, lo: float, hi: float) -> float | None:
    """0.5 단위로 반올림하고 [lo, hi]로 자른다. 숫자가 아니면 None."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):  # NaN/Inf
        return None
    v = round(v * 2.0) / 2.0
    return max(lo, min(hi, v))
