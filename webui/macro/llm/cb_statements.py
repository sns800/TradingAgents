# ============================================================
# [모듈 개요] 중앙은행 결정문 → 통화정책 기조(cb_stance) 문서·관측치 (LLM)
#
# CONTRACT.md 7장 소스 인터페이스 + 12장 케이던스(daily 확인, 신규 문서만 LLM).
# 흐름:
#   1) sources.yaml의 statements_url(결정문 목록 페이지)을 받는다
#   2) 국가별 키워드 규칙으로 "최신 결정문" 링크를 고른다 (fail-open: 규칙이
#      맞지 않으면 일반 키워드 → 날짜가 들어간 링크 순으로 완화)
#   3) ctx.extra["seen_urls"](이미 처리한 성명 URL)에 있으면 건너뛴다
#   4) 성명 본문 HTML → 평문(최대 12,000자) → S3 원본 저장
#   5) ctx.no_llm 이거나 ctx.llm이 없으면 원문만 저장하고 문서를 만들지 않는다
#   6) Bedrock(json_schema_stance)으로 점수·방향·인용 추출
#   7) **인용문이 원문에 실제로 존재하는지** 검증 → 남은 인용 0개면 문서 폐기
#      (제안서 3.3: 점수만 저장하지 않고 인용을 반드시 함께 저장)
#   8) 문서마다 평문 본문을 ctx.extra["doc_texts"]["cb_stance/<iso>/<id>"]에 남긴다
#      (collect.py가 store.save_doc_text로 S3에 전문 보존)
#
# 유로 회원국(DE/FR/IT)은 sources.yaml에서 `refer: EU`이므로 문서를 만들지 않고
# 유로존(EU) 문서만 생성한다 — 프론트가 "유로존 공통" 배지로 참조한다.
#
# 수집기(collect.py) 호출 규약:
#   docs = cb_statements.collect_docs(countries, ctx)          -> store.put_docs
#   obs  = cb_statements.collect(countries, indicators, ctx)   -> store.put_observations
#   두 함수는 같은 ctx에서 한 번만 네트워크/LLM을 쓰고 결과를 ctx.extra에 캐시한다.
# ============================================================
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any

from macro.schema import Doc, Observation, today_str

from .bedrock_json import json_schema_stance
from .common import (
    clamp_half,
    country_iso,
    extra,
    fetch_text,
    is_euro,
    keep_valid_quotes,
    llm_of,
    log,
    record_doc_text,
    record_error,
    save_raw,
)
from .meta import load_sources
from .textutil import (
    extract_links,
    html_to_text,
    is_blocked_scheme,
    normalize_ws,
    parse_date_range,
    sha12,
)

SOURCE_NAME = "cb_statements"
CADENCE = "daily"
INDICATOR = "cb_stance"
MAX_BODY_CHARS = 12_000
_CACHE_KEY = "_cb_statements_cache"

DIRECTIONS = ("hike", "hold", "cut")
GUIDANCE = ("tightening", "neutral", "easing")

# 링크 선별 규칙: 앵커 텍스트/URL에 이 조각이 들어가면 가점을 준다.
# 국가별 규칙이 하나도 맞지 않으면 _GENERIC_KEYWORDS → 날짜 포함 링크로 완화한다.
_LINK_RULES: dict[str, list[str]] = {
    "US": ["monetary", "fomc", "pressreleases/monetary"],
    "EU": ["monetary-policy", "press/pr/date", "ecb.mp", "decisions"],
    "JP": ["mopo", "monetary policy", "statement on monetary policy", "k1"],
    "GB": ["monetary-policy-summary", "bank rate", "monetary policy summary"],
    "KR": ["통화정책방향", "통화정책", "bbs/view", "menuno=4004"],
    "CN": ["lpr", "贷款市场报价利率", "loan prime rate", "goutongjiaoliu"],
    "IN": ["pressrelease", "monetary policy statement", "bi-monthly", "pr_"],
    "BR": ["copom", "comunicado", "decisao"],
    "MX": ["anuncios-de-politica-monetaria", "politica-monetaria", "anuncio"],
    "AR": ["politica-monetaria", "prensa", "comunicado"],
    "TR": ["press-releases-on-interest-rates", "faiz", "duyuru", "para politikas"],
    "SA": ["press", "news", "repo"],
    "ZA": ["mpc", "monetary policy", "statement"],
    "ID": ["bi-rate", "siaran-pers", "ru_", "press-release"],
    "AU": ["monetary-policy-decision", "media-release", "mr-mp"],
    "CA": ["press/press-releases", "interest-rate", "fad", "policy-interest-rate"],
    "RU": ["key_rate", "keyrate", "press", "pr.aspx"],
}
_GENERIC_KEYWORDS = [
    "monetary policy", "policy decision", "interest rate", "statement",
    "press release", "communiqu", "decision", "통화정책", "결정문", "금융통화",
]
_NEGATIVE_KEYWORDS = [
    "minutes", "회의록", "subscribe", "rss", "calendar", "archive", "speech",
    "glossary", "privacy", "cookie", "sitemap", "login", "javascript",
    # 결정문이 아닌 부속 문서 (실측: 연준 'Implementation Note',
    # 캐나다 '2027 schedule' 공지가 최신 결정문보다 높은 점수를 받았다)
    "implementation note", "schedule", "annual report", "biography", "vacancy",
    "tender", "working paper", "consultation", "webcast", "podcast", "survey",
]
# 첨부 파일은 html_to_text로 읽을 수 없다(PDF 바이너리가 본문으로 들어간다)
_BINARY_SUFFIXES = (".pdf", ".hwp", ".hwpx", ".doc", ".docx", ".xls", ".xlsx", ".zip")

# RSS/Atom 폴백 파싱 (목록이 JS로 렌더링되는 은행 대응)
_FEED_MARKER_RE = re.compile(r"<(rss|feed)[\s>]", re.I)
_FEED_ITEM_RE = re.compile(r"<(?:item|entry)[^>]*>(.*?)</(?:item|entry)>", re.S | re.I)
_FEED_HREF_RE = re.compile(r"<link[^>]*?href=[\"']([^\"']+)[\"']", re.S | re.I)
_FEED_LINK_RE = re.compile(r"<link[^>]*>\s*([^<\s][^<]*?)\s*</link>", re.S | re.I)
_FEED_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
_FEED_DATE_RE = re.compile(
    r"<(?:pubDate|updated|published|dc:date)[^>]*>\s*([^<]+?)\s*</", re.I
)

# 본문·URL에서 발표일을 추출하는 패턴
_URL_DATE_PATTERNS = [
    re.compile(r"(20\d{2})[-/_]?(\d{2})[-/_]?(\d{2})"),          # 20260917, 2026-09-17
    re.compile(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})"),
]
# 6자리 축약 날짜: 일본 k260731a.pdf, ECB ecb.pr250313~... (YYMMDD)
_URL_SHORT_DATE_RE = re.compile(r"[a-z.]((?:2|3)\d)(\d{2})(\d{2})(?=[a-z~._-]|$)", re.I)
# 일자 없이 연/월만 있는 경로: 캐나다 /2026/07/...
_URL_YM_RE = re.compile(r"/(20\d{2})/(0[1-9]|1[0-2])/")
_TEXT_DATE_PATTERNS = [
    re.compile(r"\b(20\d{2})[-.](\d{1,2})[-.](\d{1,2})\b"),
    re.compile(r"\b(20\d{2})년\s*(\d{1,2})월\s*(\d{1,2})일"),
]
_TEXT_MONTH_RE = re.compile(
    r"\b(\d{1,2})?\s*(January|February|March|April|May|June|July|August|September|October"
    r"|November|December)\s*(\d{1,2})?,?\s*(20\d{2})\b",
    re.I,
)
_MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}

_SYSTEM_PROMPT = """당신은 중앙은행 성명(결정문) 분석가입니다. 주어진 원문만 근거로 판단하고,
원문에 없는 내용은 절대 추측하지 마십시오.

작업:
1) title_ko: 한국어 제목 40자 이내 (예: "연준, 기준금리 25bp 인하").
2) summary_ko: 한국어 요약 3~5문장. 결정 내용, 근거(물가·성장·고용), 향후 방향을 담습니다.
3) quotes: **원문 문장을 그대로(원어, 토씨 하나 바꾸지 말고) 2~3개** 발췌합니다.
   번역·요약·생략 부호 삽입 금지. 기조 판단의 근거가 되는 문장을 고릅니다.
4) stance_score: 통화정책 기조 점수. 0.5 단위로만 답합니다.
   -2.0 강한 완화(큰 폭 인하 또는 추가 인하 강력 시사)
   -1.0 완화(인하 또는 완화 시사)
   -0.5 완화 편향
    0.0 중립(동결 + 양방향 열어둠)
   +0.5 긴축 편향
   +1.0 긴축(인상 또는 긴축 시사)
   +2.0 강한 긴축(큰 폭 인상 또는 추가 인상 강력 시사)
5) direction: 이번 회의의 결정. hike(인상) | hold(동결) | cut(인하)
6) forward_guidance: 향후 방향. tightening | neutral | easing
7) rate_after: 결정 후 정책금리(%) 숫자. 원문에 없으면 생략.
8) statement_date: 성명 발표일 YYYY-MM-DD. 확실하지 않으면 생략.
9) meeting_type: regular | unscheduled | minutes | other
10) confidence: 0~1. 원문이 불완전하거나 기조가 모호하면 낮게 줍니다."""


# ---------------------------------------------------------------- 공개 API
def collect_docs(countries: list[Any], ctx: Any) -> list[Doc]:
    """신규 결정문에서 cb_stance 문서를 만든다 (store.put_docs 용)."""
    return _run(countries, ctx)[0]


def collect(countries: list[Any], indicators: list[Any], ctx: Any) -> list[Observation]:
    """cb_stance 관측치(freq E, period=성명일, value=stance_score, unit score)."""
    return _run(countries, ctx)[1]


def _run(countries: list[Any], ctx: Any) -> tuple[list[Doc], list[Observation]]:
    """collect/collect_docs가 같은 ctx에서 중복 수집하지 않도록 캐시한다."""
    box = getattr(ctx, "extra", None)
    if isinstance(box, dict) and _CACHE_KEY in box:
        cached = box[_CACHE_KEY]
        if isinstance(cached, tuple) and len(cached) == 2:
            return cached
    result = _collect_all(countries, ctx)
    if isinstance(box, dict):
        box[_CACHE_KEY] = result
    return result


def _collect_all(countries: list[Any], ctx: Any) -> tuple[list[Doc], list[Observation]]:
    meta_all = load_sources().get("central_banks", {})
    seen_urls = extra(ctx, "seen_urls", set())
    docs: list[Doc] = []
    obs: list[Observation] = []
    llm = llm_of(ctx)

    for country in countries:
        iso = country_iso(country)
        if not iso:
            continue
        meta = meta_all.get(iso) or {}
        if not meta:
            continue
        if meta.get("refer"):
            # 유로 회원국: EU 문서를 참조하므로 자체 문서를 만들지 않는다
            continue
        if is_euro(country) and iso != "EU":
            continue
        try:
            d, o = _collect_country(iso, meta, ctx, llm, seen_urls)
        except Exception as exc:  # noqa: BLE001 - 국가 단위 실패 격리 (CONTRACT 7장)
            record_error(ctx, SOURCE_NAME, iso, f"수집 실패: {exc}")
            continue
        docs.extend(d)
        obs.extend(o)
    return docs, obs


# ---------------------------------------------------------------- 국가 단위
def _collect_country(
    iso: str, meta: dict[str, Any], ctx: Any, llm: Any, seen_urls: Any
) -> tuple[list[Doc], list[Observation]]:
    list_url = meta.get("statements_url")
    if not list_url:
        return [], []
    ua = meta.get("user_agent")
    index_html = fetch_text(ctx, SOURCE_NAME, iso, list_url, user_agent=ua)
    if index_html is None:
        return [], []
    save_raw(ctx, SOURCE_NAME, f"{iso}_index", index_html)

    max_docs = int(meta.get("max_docs", 1) or 1)
    keywords = meta.get("link_keywords")
    links = _links_from_html_or_feed(index_html, list_url, iso, max_docs, keywords)
    if not links and meta.get("rss"):
        # 목록이 JS 렌더링이면 피드로 재시도한다 (실측: EU·GB·IN·ZA)
        feed_xml = fetch_text(ctx, SOURCE_NAME, iso, str(meta["rss"]), user_agent=ua)
        if feed_xml:
            save_raw(ctx, SOURCE_NAME, f"{iso}_feed", feed_xml)
            links = pick_statement_links(
                "", str(meta["rss"]), iso, limit=max_docs, extra_keywords=keywords,
                candidates=links_from_feed(feed_xml, str(meta["rss"])),
            )
    if not links:
        record_error(
            ctx, SOURCE_NAME, iso, f"결정문 링크를 찾지 못함(목록 JS 렌더링 가능): {list_url}"
        )
        return [], []

    docs: list[Doc] = []
    obs: list[Observation] = []
    for url, anchor in links:
        if url in seen_urls:
            log(ctx, f"[{SOURCE_NAME}] {iso} 이미 처리한 성명 건너뜀: {url}")
            continue
        body_html = fetch_text(ctx, SOURCE_NAME, iso, url, user_agent=ua)
        if body_html is None:
            continue
        text = html_to_text(body_html, max_chars=MAX_BODY_CHARS)
        if len(text) < 200:
            record_error(ctx, SOURCE_NAME, iso, f"본문이 너무 짧음({len(text)}자): {url}")
            continue
        save_raw(ctx, SOURCE_NAME, f"{iso}_{sha12(url)}", body_html)

        if llm is None:
            log(ctx, f"[{SOURCE_NAME}] {iso} no_llm: 원문만 저장 ({url})")
            continue

        doc = _build_doc(iso, meta, url, anchor, text, llm, ctx)
        if doc is None:
            continue
        # 수집기가 S3 전문(macro/docs/...)으로 올릴 성명 원문 (LLM에 넣은 평문 그대로)
        record_doc_text(ctx, doc, text)
        docs.append(doc)
        ob = _build_observation(iso, doc)
        if ob is not None:
            obs.append(ob)
    return docs, obs


def _links_from_html_or_feed(
    index_text: str, list_url: str, iso: str, limit: int, keywords: list[str] | None
) -> list[tuple[str, str]]:
    """목록이 HTML이면 앵커에서, 피드(RSS/Atom)면 항목에서 링크를 고른다."""
    if _FEED_MARKER_RE.search(index_text[:4000]):
        return pick_statement_links(
            "", list_url, iso, limit=limit, extra_keywords=keywords,
            candidates=links_from_feed(index_text, list_url),
        )
    return pick_statement_links(
        index_text, list_url, iso, limit=limit, extra_keywords=keywords
    )


def _build_doc(
    iso: str, meta: dict[str, Any], url: str, anchor: str, text: str, llm: Any, ctx: Any
) -> Doc | None:
    user = (
        f"[중앙은행] {meta.get('name_en') or meta.get('name_ko') or iso}\n"
        f"[국가코드] {iso}\n[링크 제목] {anchor}\n[원문 URL] {url}\n\n[성명 원문]\n{text}"
    )
    try:
        raw = llm.invoke_json(
            system=_SYSTEM_PROMPT,
            user=user,
            schema=json_schema_stance,
            tool_name="emit_stance",
            max_tokens=1600,
        )
    except Exception as exc:  # noqa: BLE001 - 예산 초과/스키마 실패도 국가 격리
        record_error(ctx, SOURCE_NAME, iso, f"LLM 판정 실패 {url}: {exc}")
        return None

    quotes = keep_valid_quotes(raw.get("quotes"), text, max_n=3)
    if not quotes:
        record_error(
            ctx, SOURCE_NAME, iso, f"인용문이 원문에 없어 문서 폐기: {url}"
        )
        return None

    score = clamp_half(raw.get("stance_score"), -2.0, 2.0)
    if score is None:
        record_error(ctx, SOURCE_NAME, iso, f"stance_score가 숫자가 아님: {url}")
        return None
    direction = _enum_or_infer(raw.get("direction"), DIRECTIONS, score, ("hike", "hold", "cut"))
    guidance = _enum_or_infer(
        raw.get("forward_guidance"), GUIDANCE, score, ("tightening", "neutral", "easing")
    )
    stmt_date = _statement_date(raw.get("statement_date"), url, text)
    confidence = _confidence(raw.get("confidence"))
    rate_after = _float_or_none(raw.get("rate_after"))
    meeting_type = normalize_ws(str(raw.get("meeting_type") or "regular"))[:40]
    title = normalize_ws(str(raw.get("title_ko") or ""))[:120] or f"{meta.get('name_ko', iso)} 결정문"
    summary = normalize_ws(str(raw.get("summary_ko") or ""))

    return Doc(
        type="cb_stance",
        iso=iso,
        date=stmt_date,
        id=sha12(url),
        title_ko=title,
        summary_ko=summary,
        source_url=url,
        source_name=str(meta.get("name_ko") or meta.get("name_en") or iso),
        payload={
            "stance_score": score,
            "direction": direction,
            "forward_guidance": guidance,
            "rate_after": rate_after,
            "statement_date": stmt_date,
            "meeting_type": meeting_type,
        },
        ai_generated=True,
        model_id=getattr(llm, "model_id", None),
        confidence=confidence,
        quotes=quotes,
        s3_key=f"macro/docs/cb_stance/{iso}/{stmt_date}_{sha12(url)}.md",
    )


def _build_observation(iso: str, doc: Doc) -> Observation | None:
    score = doc.payload.get("stance_score")
    if score is None:
        return None
    return Observation(
        indicator=INDICATOR,
        iso=iso,
        freq="E",
        period=doc.date,
        value=float(score),
        unit="score",
        source=SOURCE_NAME,
        series_id=f"{iso}:cb_stance:{doc.id}",
        source_url=doc.source_url,
        method="중앙은행 결정문 LLM 판정 (-2 강한 완화 ~ +2 강한 긴축, 0.5 단위)",
        payload={
            "direction": doc.payload.get("direction"),
            "forward_guidance": doc.payload.get("forward_guidance"),
            "doc_id": doc.id,
        },
        vintage=doc.date,
        flags=["ai_generated"],
    )


# ---------------------------------------------------------------- 링크 선별
def pick_statement_links(
    html: str,
    base_url: str,
    iso: str,
    *,
    limit: int = 1,
    extra_keywords: list[str] | None = None,
    candidates: list[tuple[str, str]] | None = None,
) -> list[tuple[str, str]]:
    """결정문 목록 페이지에서 **가장 최신** 결정문 링크를 고른다 (fail-open).

    candidates를 주면 HTML 앵커 대신 그 (링크, 텍스트) 목록을 후보로 쓴다
    (RSS/Atom 폴백 경로).

    실측(17개 중앙은행)으로 정한 규칙:
      - 목록 페이지 자신(같은 경로)과 PDF·HWP 등 첨부는 후보에서 제외한다.
        같은 경로를 고르면 목록 텍스트가 LLM에 들어가 엉뚱한 판정이 나온다.
      - 국가 키워드(sources.yaml link_keywords + _LINK_RULES)가 맞는 링크를 먼저
        고르고, **그 안에서 URL/앵커의 날짜가 가장 최신인 것**을 쓴다. 목록 순서만
        믿으면 연준처럼 연초 성명이 먼저 나오는 페이지에서 오래된 문서를 집는다.
      - sources.yaml에 link_keywords가 있으면 **그 패턴만** 쓴다(strict). 없으면
        일반 키워드 → 날짜가 있는 링크로 단계적으로 완화한다(미설정 국가 fail-open).
    """
    links = candidates if candidates is not None else extract_links(html, base_url)
    if not links:
        return []
    base_path = _url_path(base_url)
    # sources.yaml이 link_keywords를 주면 그것만 쓰고(모듈 기본값보다 정밀) 완화 단계도
    # 끄는 strict 모드로 동작한다. 실측 결과 확인된 문제: 목록이 JS로 렌더링되는
    # 은행(영국·브라질·멕시코·사우디)에서 완화 단계가 'SONIA 벤치마크'·'통화정책 개요'
    # 같은 **엉뚱한 페이지**를 집어 LLM이 결정문이 아닌 문서를 판정하게 만든다.
    # 링크를 못 찾고 오류로 남기는 편이 잘못된 문서를 만드는 것보다 낫다.
    given = [str(k).lower() for k in (extra_keywords or []) if k]
    strict = bool(given)
    rules = given or [k.lower() for k in _LINK_RULES.get(iso, [])]

    cands: list[tuple[int, int, int, int, str, str]] = []
    for order, (href, anchor) in enumerate(links):
        clean = href.split("#")[0]
        low = clean.lower()
        if low.endswith(_BINARY_SUFFIXES):
            continue
        if _url_path(clean) == base_path:
            continue  # 목록 페이지 자신 (쿼리만 다른 경우도 제외)
        blob = f"{clean} {anchor}".lower()
        if any(neg in blob for neg in _NEGATIVE_KEYWORDS):
            continue
        rule_hits = sum(1 for k in rules if k in blob)
        generic_hits = sum(1 for k in _GENERIC_KEYWORDS if k in blob)
        found = _date_from_url(clean, allow_month_only=True) or _date_from_text(anchor)
        if strict and not rule_hits:
            continue
        if not (rule_hits or generic_hits or found):
            continue
        tier = 0 if rule_hits else (1 if generic_hits else 2)
        cands.append(
            (tier, -(found.toordinal() if found else 0), -rule_hits, order, clean, anchor)
        )
    if not cands:
        return []
    cands.sort(key=lambda t: t[:4])
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for _t, _d, _r, _o, href, anchor in cands:
        if href in seen:
            continue
        seen.add(href)
        out.append((href, anchor))
        if len(out) >= max(1, limit):
            break
    return out


def links_from_feed(xml: str, base_url: str | None = None) -> list[tuple[str, str]]:
    """RSS/Atom 문서에서 (링크, "제목 YYYY-MM-DD") 목록을 뽑는다.

    영국·유럽중앙은행·인도·남아공은 결정문 목록이 JS로 렌더링되어 HTML에 링크가
    없지만(실측) 피드는 서버가 채워 보낸다. 피드 항목 날짜를 앵커 텍스트에 이어
    붙여 기존 링크 선별 로직(최신 우선)이 그대로 동작하게 한다.
    """
    out: list[tuple[str, str]] = []
    for body in _FEED_ITEM_RE.findall(xml or ""):
        m = _FEED_HREF_RE.search(body) or _FEED_LINK_RE.search(body)
        if not m:
            continue
        href = normalize_ws(m.group(1))
        if not href or is_blocked_scheme(href):
            continue  # javascript:/data:/vbscript: 링크는 후보에서 제외 (프론트 노출 방지)
        if base_url and not href.lower().startswith(("http://", "https://")):
            from urllib.parse import urljoin

            href = urljoin(base_url, href)
        if is_blocked_scheme(href):
            continue
        title = ""
        tm = _FEED_TITLE_RE.search(body)
        if tm:
            title = normalize_ws(re.sub(r"<[^>]+>", " ", tm.group(1)))
            title = title.replace("<![CDATA[", "").replace("]]>", "").strip()
        dm = _FEED_DATE_RE.search(body)
        stamp = ""
        if dm:
            _st, end = parse_date_range(dm.group(1))
            if end is not None:
                stamp = end.isoformat()
        out.append((href, normalize_ws(f"{title} {stamp}")))
    return out


def _url_path(url: str) -> str:
    """비교용 경로 (스킴·쿼리·프래그먼트·끝 슬래시 제거)."""
    from urllib.parse import urlsplit

    parts = urlsplit(url.split("#")[0])
    return f"{parts.netloc}{parts.path}".rstrip("/").lower()


# ---------------------------------------------------------------- 값 정리
def _enum_or_infer(
    value: Any, allowed: tuple[str, ...], score: float, triple: tuple[str, str, str]
) -> str:
    """열거값이 아니면 stance_score 부호로 추론한다 (up, flat, down 순서)."""
    v = str(value or "").strip().lower()
    if v in allowed:
        return v
    if score >= 0.5:
        return triple[0]
    if score <= -0.5:
        return triple[2]
    return triple[1]


def _float_or_none(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _confidence(value: Any) -> float:
    f = _float_or_none(value)
    if f is None:
        return 0.5
    return max(0.0, min(1.0, f))


def _mk(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _date_from_url(url: str, *, allow_month_only: bool = False) -> date | None:
    """URL 안의 날짜를 뽑는다.

    20260917 / 2026-09-17 / 2026/09/17 → 정확한 날짜.
    k260731a.pdf, ecb.pr250313 같은 6자리 축약(YYMMDD)도 해석한다.
    allow_month_only이면 /2026/07/ 처럼 일자가 없는 경로도 그 달 1일로 돌려준다
    (링크 정렬용이며 문서 날짜로는 쓰지 않는다).
    """
    for pat in _URL_DATE_PATTERNS:
        for m in pat.finditer(url):
            got = _mk(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if got and 2000 <= got.year <= 2100:
                return got
    for m in _URL_SHORT_DATE_RE.finditer(url):
        got = _mk(2000 + int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if got and 2000 <= got.year <= 2100:
            return got
    if allow_month_only:
        m = _URL_YM_RE.search(url)
        if m:
            return _mk(int(m.group(1)), int(m.group(2)), 1)
    return None


def _date_from_text(text: str) -> date | None:
    for pat in _TEXT_DATE_PATTERNS:
        m = pat.search(text)
        if m:
            got = _mk(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if got:
                return got
    m = _TEXT_MONTH_RE.search(text)
    if m:
        d1, mon, d2, year = m.groups()
        day = d1 or d2
        mm = _MONTH_NAMES.get((mon or "").lower())
        if mm and day:
            got = _mk(int(year), mm, int(day))
            if got:
                return got
    return None


def _statement_date(llm_value: Any, url: str, text: str) -> str:
    """성명 발표일: LLM 값 → URL 패턴 → 본문 앞부분 → 오늘."""
    today = datetime.now(timezone.utc).date()
    cand = None
    raw = str(llm_value or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        cand = _mk(int(raw[:4]), int(raw[5:7]), int(raw[8:10]))
    if cand is None:
        cand = _date_from_text(text[:2000])
    if cand is None:
        cand = _date_from_url(url)
    if cand is None or cand > today or cand.year < 2000:
        return today_str()
    return cand.isoformat()
