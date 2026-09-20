# ============================================================
# [모듈 개요] 위키피디아 여론조사 표 → poll/election 문서 + party_support 관측치
#
# CONTRACT.md 7장 인터페이스, 12장 케이던스(weekly). 설계 원칙:
#   1차는 **규칙 기반 파싱**(비용 0, 재현 가능). LLM은 헤더 병합·주석 때문에
#   규칙이 0건을 낸 표에만 보조로 쓴다(json_schema_polls). ctx.no_llm이면 규칙만.
#
# 표 선별 heuristic (실측 기반: 한국·영국·독일 페이지 구조 확인):
#   - class="wikitable" 중 (날짜 계열 컬럼 + 정당 컬럼 2개 이상)을 가진 표
#   - 연도 절 제목("2026", "2025")을 가진 표가 있으면 그것만 최신 연도순으로
#     max_tables개 사용 → 좌석 예측(Seat projections)·지역(Scotland/Wales)·
#     가정 시나리오 표가 자동으로 걸러진다
#   - 날짜 칸에 연도가 없는 표("8–10 Sep")는 절 제목의 연도로 보정한다
#
# 검증(하나라도 실패하면 그 조사만 버림):
#   results 값 0~100 · 합계 ≤ 105 · fieldwork_end 유효·미래 아님·400일 이내
#   정당명은 party_aliases로 한국어 표준명 매핑(매핑 실패 시 원문 헤더 유지)
#
# poll_of_polls(30일 가중 이동평균)는 수집기의 aggregate.poll_of_polls 담당이며
# 이 모듈은 계산하지 않는다.
#
# 수집기(collect.py) 호출 규약:
#   docs = polls_wiki.collect_docs(countries, ctx)          -> store.put_docs
#   obs  = polls_wiki.collect(countries, indicators, ctx)   -> store.put_observations
# ============================================================
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timezone
from typing import Any

from macro.schema import Doc, Observation, today_str

from .bedrock_json import json_schema_polls
from .common import (
    country_iso,
    country_name_ko,
    fetch_text,
    keep_valid_quotes,
    llm_of,
    log,
    record_error,
    save_raw,
)
from .meta import load_sources
from .textutil import (
    Table,
    extract_tables,
    normalize_ws,
    parse_date_range,
    parse_int,
    parse_percent,
    sha12,
)

SOURCE_NAME = "wiki_polls"
CADENCE = "weekly"
WIKI_BASE = "https://en.wikipedia.org/wiki/"
MAX_AGE_DAYS = 400
MAX_RESULT_SUM = 105.0
MAX_TABLE_CHARS = 8_000
DEFAULT_MAX_TABLES = 2
DEFAULT_MAX_ROWS = 40
MIN_EXTRA_ROWS = 5
_CACHE_KEY = "_wiki_polls_cache"

# 정당이 아닌 컬럼. 정규화 헤더가 이 집합과 **완전히 일치**할 때만 버린다
# (부분 일치로 버리면 영국의 "Ref"=Reform UK 같은 실제 정당이 사라진다).
_EXCLUDE_EXACT = {
    "lead", "leads", "others", "other", "abs", "abstention", "abstentions",
    "undecided", "und", "und/noans", "undnoans", "dontknow", "noanswer", "none",
    "majority", "client", "area", "method", "methodology", "source", "sources",
    "note", "notes", "ref", "refs", "reference", "turnout", "gap", "swing",
    "difference", "net", "total", "seats", "date", "dates", "n", "na",
}
# 부분 일치로 버려도 안전한 헤더 조각
_EXCLUDE_SUBSTR = (
    "margin of error", "margin", "sample", "표본", "폴스터", "seat projection",
    "electorate", "field work", "fieldwork", "polling firm", "pollster",
    "conducted", "publication", "released", "commission",
)
_DATE_KEYS = ("date", "fieldwork", "field work", "period", "conducted", "조사", "기간")
_SAMPLE_KEYS = ("sample", "표본", "n=")
_POLLSTER_KEYS = (
    "pollster", "polling firm", "poll firm", "pollingfirm", "institute", "agency",
    "organisation", "organization", "company", "기관", "조사기관",
)
_APPROVAL_KEYS = ("approval", "approve", "국정지지", "지지도")
# 여론조사가 아닌 행(실제 선거 결과, 출구조사 등)
_NON_POLL_ROW = re.compile(
    r"(election|referendum|exit poll|결과|선거|resultado|wahl|1st round|2nd round)", re.I
)
# 전국 정당지지율이 아닌 표의 제목 조각 (단어 경계로 비교 — "men"이 "Government"에
# 걸리면 안 된다). 실측 근거: 호주 'Women'/'Men' 성별 분해표, 캐나다 '선호 총리',
# 영국 'Seat projections', 각국 가정 시나리오 표가 전국 표보다 최신·대량인 경우가 있다.
_HEADING_SKIP = (
    "seat projection", "seat projections", "seats", "coalition", "coalitions",
    "hypothetical", "women", "men", "male", "female", "gender", "age", "age group",
    "demographic", "demographics", "constituency", "constituencies", "runoff",
    "run-off", "second round", "preferred", "leader", "leadership", "by state",
    "regional", "generation", "generation z", "millennials", "boomers", "gen x",
)
_YEAR_HEADING_RE = re.compile(r"^(19|20)\d{2}$")
_HEADING_SKIP_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in _HEADING_SKIP) + r")\b", re.I
)


# 연령대 분해표 제목("18–34", "65+")은 단어가 아니라 숫자 구간이라 별도 규칙이 필요하다.
# (연도 제목 "2026"은 4자리 단독이라 걸리지 않는다.)
_AGE_BRACKET_RE = re.compile(r"^\s*\d{1,2}\s*(?:[-+~]|to)\s*\d{0,2}\s*\+?\s*$")


def heading_blocked(heading: str, extra: set[str] | None = None) -> bool:
    """표 제목이 전국 정당지지율 표가 아님을 시사하는지 판정한다."""
    h = normalize_ws(heading or "")
    if not h:
        return False
    if _AGE_BRACKET_RE.match(h.translate(str.maketrans("–—−", "---"))):
        return True
    if _HEADING_SKIP_RE.search(h):
        return True
    low = h.lower()
    return any(k and k.lower() in low for k in (extra or ()))

_SYSTEM_PROMPT = """당신은 위키피디아 여론조사 표를 정규화하는 데이터 추출기입니다.
주어진 표 텍스트에 실제로 적혀 있는 값만 옮기고, 없는 값은 생략하십시오. 추정·보간 금지.

- pollster: 조사기관 이름(표기 그대로).
- fieldwork_start / fieldwork_end: YYYY-MM-DD. 표에 연도가 없으면 안내된 기준 연도를 씁니다.
- sample_size: 표본 수 정수. 없으면 생략.
- results: {정당 컬럼 헤더: 지지율 숫자}. '—', 'N/a', 빈칸은 넣지 않습니다.
  Others/Undecided/Lead/Margin of error/표본 컬럼은 results에 넣지 않습니다.
- gov_approval: 정부(대통령/총리) 지지율 컬럼이 있을 때만.
- method: 조사 방식이 적혀 있을 때만.
- row_text: 그 조사에 해당하는 **표 원문 행 텍스트를 그대로** 복사합니다(검증용, 필수).

실제 선거 결과 행(예: '2025 federal election')은 여론조사가 아니므로 제외합니다."""


# ---------------------------------------------------------------- 공개 API
def collect_docs(countries: list[Any], ctx: Any) -> list[Doc]:
    """poll / election / not_applicable 문서 (store.put_docs 용)."""
    return _run(countries, ctx)[0]


def collect(countries: list[Any], indicators: list[Any], ctx: Any) -> list[Observation]:
    """party_support (freq W) + gov_approval 관측치 (store.put_observations 용)."""
    return _run(countries, ctx)[1]


def _run(countries: list[Any], ctx: Any) -> tuple[list[Doc], list[Observation]]:
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
    meta_all = load_sources().get("polls", {})
    docs: list[Doc] = []
    obs: list[Observation] = []
    llm = llm_of(ctx)
    for country in countries:
        iso = country_iso(country)
        meta = meta_all.get(iso) or {}
        if not meta:
            continue
        try:
            d, o = _collect_country(country, iso, meta, ctx, llm)
        except Exception as exc:  # noqa: BLE001 - 국가 단위 실패 격리 (CONTRACT 7장)
            record_error(ctx, SOURCE_NAME, iso, f"수집 실패: {exc}")
            continue
        docs.extend(d)
        obs.extend(o)
    return docs, obs


def _collect_country(
    country: Any, iso: str, meta: dict[str, Any], ctx: Any, llm: Any
) -> tuple[list[Doc], list[Observation]]:
    name_ko = country_name_ko(country)
    reason = meta.get("not_applicable_reason")
    if reason:
        return [_not_applicable_doc(iso, name_ko, str(reason), meta)], []

    docs: list[Doc] = []
    election_doc = _election_doc(iso, name_ko, meta)
    if election_doc is not None:
        docs.append(election_doc)

    page = meta.get("wiki_page")
    if not page:
        record_error(ctx, SOURCE_NAME, iso, "sources.yaml에 wiki_page가 없어 여론조사 생략")
        return docs, []

    url = page if str(page).startswith("http") else WIKI_BASE + str(page)
    html = fetch_text(ctx, SOURCE_NAME, iso, url)
    if html is None:
        return docs, []
    save_raw(ctx, SOURCE_NAME, f"{iso}_polls", html)

    alias_map = build_alias_map(meta.get("party_aliases") or {})
    exclude = {_alias_key(x) for x in (meta.get("exclude_columns") or []) if x}
    heading_exclude = {str(x) for x in (meta.get("heading_exclude") or []) if x}
    tables = select_poll_tables(
        extract_tables(html, css_class="wikitable"),
        alias_map,
        max_tables=int(meta.get("max_tables", DEFAULT_MAX_TABLES) or DEFAULT_MAX_TABLES),
        exclude=exclude,
        heading_exclude=heading_exclude,
    )
    if not tables:
        record_error(ctx, SOURCE_NAME, iso, f"여론조사 표를 찾지 못함: {url}")
        return docs, []

    max_rows = int(meta.get("max_rows", DEFAULT_MAX_ROWS) or DEFAULT_MAX_ROWS)
    polls: list[dict[str, Any]] = []
    for table in tables:
        rows = parse_poll_table(table, alias_map, max_rows=max_rows, exclude=exclude)
        if rows:
            polls.extend(rows)
            continue
        log(ctx, f"[{SOURCE_NAME}] {iso} 규칙 파싱 0건 → LLM 보조 (절 '{table.heading}')")
        if llm is None:
            continue
        polls.extend(_llm_table(iso, table, alias_map, llm, ctx, url))

    seen: set[tuple[str, str]] = set()
    obs: list[Observation] = []
    for poll in polls:
        key = (poll["pollster"].lower(), poll["fieldwork_end"])
        if key in seen:
            continue
        seen.add(key)
        doc = _poll_doc(iso, name_ko, meta, poll, url)
        docs.append(doc)
        obs.extend(_poll_observations(iso, meta, poll, url, doc.id))
    return docs, obs


# ---------------------------------------------------------------- 표 선별·파싱
def build_alias_map(party_aliases: dict[str, Any]) -> dict[str, str]:
    """{표준명_ko: [별칭...]} → {정규화 별칭 키: 표준명_ko}."""
    out: dict[str, str] = {}
    for ko, aliases in (party_aliases or {}).items():
        names = [ko] + list(aliases or [])
        for alias in names:
            key = _alias_key(alias)
            if key and key not in out:
                out[key] = ko
    return out


def _alias_key(s: Any) -> str:
    """별칭 비교용 키: NFKC → 소문자 → 영숫자/한글만 남김."""
    t = unicodedata.normalize("NFKC", str(s or "")).lower()
    return re.sub(r"[^0-9a-z가-힣ㄱ-ㅎÀ-ɏ]", "", t)


def match_alias(header: str, alias_map: dict[str, str]) -> str | None:
    """헤더를 표준 정당명으로 매핑한다. 전체 일치 → 토큰 단위 일치 순.

    토큰 폴백이 필요한 이유(실측): 호주 표 헤더는 "Primary vote ALP",
    브라질은 "Lula PT"처럼 정당 약칭이 문구 안에 들어 있다. 전체 키만 보면
    매핑이 전부 실패해 정당 시계열이 만들어지지 않는다.
    """
    whole = _alias_key(header)
    if whole and whole in alias_map:
        return alias_map[whole]
    for token in re.split(r"[\s/()\[\],|·-]+", unicodedata.normalize("NFKC", header or "")):
        key = _alias_key(token)
        if len(key) >= 2 and key in alias_map:
            return alias_map[key]
    return None


def _hdr_norm(s: str) -> str:
    return normalize_ws(unicodedata.normalize("NFKC", s or "")).lower().rstrip(":%").strip()


def classify_columns(
    table: Table, alias_map: dict[str, str], exclude: set[str] | None = None
) -> dict[str, Any]:
    """헤더+데이터를 보고 컬럼 역할을 판정한다.

    반환: {"pollster": idx|None, "date": idx|None, "sample": idx|None,
            "approval": idx|None, "parties": {idx: 표준명_ko 또는 원문 헤더},
            "mapped": 별칭으로 표준명이 붙은 정당 컬럼 수}
    정당 판정 우선순위: party_aliases 일치 > exclude/제외 목록 > 숫자 비율 heuristic.
    exclude는 sources.yaml의 exclude_columns (예: 이탈리아 CSX/CDX 연합 합계 컬럼).
    """
    headers = table.headers()
    data = table.data_rows()
    exclude = exclude or set()
    roles: dict[str, Any] = {
        "pollster": None, "date": None, "sample": None, "approval": None,
        "parties": {}, "mapped": 0,
    }
    for c, raw in enumerate(headers):
        hn = _hdr_norm(raw)
        hk = _alias_key(raw)
        tokens = {_alias_key(x) for x in re.split(r"[\s/()\[\],|·]+", hn) if x}
        if hk in exclude or hn in exclude or (tokens & exclude):
            continue
        std = match_alias(raw, alias_map)
        if std is not None:
            # 같은 표준명에 두 컬럼이 매핑되면(예: 1차 지지 + 양당선호) 첫 컬럼만 쓴다
            if std not in roles["parties"].values():
                roles["parties"][c] = std
                roles["mapped"] += 1
            continue
        if any(k in hn for k in _APPROVAL_KEYS):
            if roles["approval"] is None:
                roles["approval"] = c
            continue
        if any(k in hn for k in _SAMPLE_KEYS):
            if roles["sample"] is None:
                roles["sample"] = c
            continue
        if any(k in hn for k in _POLLSTER_KEYS):
            if roles["pollster"] is None:
                roles["pollster"] = c
            continue
        if any(k in hn for k in _DATE_KEYS):
            if roles["date"] is None:
                roles["date"] = c
            continue
        # 2행 헤더가 합쳐지면 "Parties Lead"·"Primary vote Others"처럼 그룹 이름이
        # 앞에 붙는다(이탈리아·호주 실측) → 토큰 단위로도 제외 판정한다
        if hk in _EXCLUDE_EXACT or hn in _EXCLUDE_EXACT or (tokens & _EXCLUDE_EXACT):
            continue
        if any(k in hn for k in _EXCLUDE_SUBSTR):
            continue
        if _looks_numeric_column(data, c) and len(hn) <= 24:
            roles["parties"][c] = raw or f"col{c}"
    if roles["date"] is None:
        # 헤더에 날짜 키워드가 없는 표: 데이터가 날짜로 읽히는 첫 컬럼을 쓴다
        for c in range(table.n_cols):
            if c in roles["parties"] or c in (roles["sample"], roles["pollster"]):
                continue
            hits = sum(1 for r in data[:8] if parse_date_range(_cell(r, c))[1] is not None)
            if hits >= 2:
                roles["date"] = c
                break
    if roles["pollster"] is None:
        for c in range(table.n_cols):
            if c in roles["parties"] or c in (roles["date"], roles["sample"]):
                continue
            roles["pollster"] = c  # fail-open: 남은 첫 컬럼을 조사기관으로 본다
            break
    return roles


def _cell(row: list[str], idx: int | None) -> str:
    if idx is None or idx < 0 or idx >= len(row):
        return ""
    return row[idx]


def _looks_numeric_column(data: list[list[str]], c: int) -> bool:
    """데이터 셀의 절반 이상이 0~100 백분율로 읽히면 정당(수치) 컬럼으로 본다."""
    sample = data[:12]
    if not sample:
        return False
    ok = 0
    for row in sample:
        v = parse_percent(_cell(row, c))
        if v is not None and 0.0 <= v <= 100.0:
            ok += 1
    return ok >= max(2, (len(sample) + 1) // 2)


def looks_like_poll_table(
    table: Table,
    alias_map: dict[str, str],
    exclude: set[str] | None = None,
    heading_exclude: set[str] | None = None,
) -> bool:
    """날짜 계열 컬럼 + 정당 컬럼 2개 이상이면 여론조사 표 후보."""
    if len(table.rows) < 2 or table.n_cols < 4:
        return False
    if heading_blocked(table.heading, heading_exclude):
        return False
    roles = classify_columns(table, alias_map, exclude)
    return roles["date"] is not None and len(roles["parties"]) >= 2


def table_latest_end(
    table: Table,
    roles: dict[str, Any],
    *,
    today: date | None = None,
) -> int:
    """표 앞부분 행에서 읽어낸 가장 최근 조사 종료일의 서수 (없으면 0).

    표 선택의 1순위 신호다. 연도 절 제목만으로는 부족한 이유(실측): 브라질
    페이지는 선거 국면별('Polling aggregation', 'Aug–Oct Campaign')로 표를
    나누어 최신 표에 연도 제목이 없고, 연도 제목이 붙은 표는 과거 연도다.
    """
    idx = roles.get("date")
    if idx is None:
        return 0
    hint = table.context_year()
    best = 0
    for row in table.data_rows()[:8]:
        _st, end = _row_dates(_cell(row, idx), hint, today=today)
        if end is not None:
            best = max(best, end.toordinal())
    return best


def _recency_bucket(latest_ordinal: int, today: date | None = None) -> int:
    """최신 조사일이 얼마나 최근인지 (0=120일 이내, 1=400일 이내, 2=그 밖)."""
    if latest_ordinal <= 0:
        return 2
    now = (today or datetime.now(timezone.utc).date()).toordinal()
    age = now - latest_ordinal
    if age <= 120:
        return 0
    if age <= MAX_AGE_DAYS:
        return 1
    return 2


def select_poll_tables(
    tables: list[Table],
    alias_map: dict[str, str],
    *,
    max_tables: int = DEFAULT_MAX_TABLES,
    exclude: set[str] | None = None,
    heading_exclude: set[str] | None = None,
    today: date | None = None,
) -> list[Table]:
    """후보 표를 정렬해 앞에서 max_tables개를 고른다.

    정렬 키 (20개국 실측으로 조정):
      1) 별칭이 하나도 안 붙은 표는 뒤로 — 캐나다는 '선호 총리' 표가 정당 표보다
         앞에 나오는데 후자에만 LPC/CPC 별칭이 붙는다.
      2) 최신 조사일 구간(120일/400일) 오름차순 — 과거 연도 표가 뒤로 간다.
         브라질처럼 최신 표에 연도 제목이 없고 과거 표에만 있는 경우를 잡는다.
      3) 제목이 연도 단독("2026")인 표 우선 — 각국 페이지의 전국 주력 표 관례다.
      4) 데이터 행 수(20행 이상은 동급) 내림차순 — 전국 주력 표는 행이 많다.
      5) 별칭 매핑 수 내림차순 — 프랑스처럼 같은 페이지에 후보 조합이 다른 표가
         여러 개 있을 때 표준 정당이 가장 많이 붙는 표를 고른다.
      6) 문서 등장 순서.
    제목 자체가 지역·성별·좌석예측·선호총리 표임을 말해주면 후보에서 제외한다
    (_HEADING_SKIP + sources.yaml의 heading_exclude).
    """
    scored: list[tuple[int, int, int, int, int, int, Table]] = []
    for i, t in enumerate(tables):
        if not looks_like_poll_table(t, alias_map, exclude, heading_exclude):
            continue
        roles = classify_columns(t, alias_map, exclude)
        mapped = int(roles["mapped"])
        rows_bucket = min(len(t.data_rows()), 20) // 5
        latest = table_latest_end(t, roles, today=today)
        is_year = 0 if _YEAR_HEADING_RE.match(normalize_ws(t.heading)) else 1
        scored.append(
            (
                0 if mapped else 1,
                _recency_bucket(latest, today),
                is_year,
                -rows_bucket,
                -mapped,
                i,
                t,
            )
        )
    scored.sort(key=lambda x: x[:6])
    if not scored:
        return []
    # 1순위 표는 조건 없이 쓰고, 2순위부터는 행이 MIN_EXTRA_ROWS 이상인 표만 더한다.
    # (영국 'Clacton' 같은 선거구 단독 조사 1행 표가 전국 지지율에 섞이는 것을 막는다)
    out = [scored[0][6]]
    for item in scored[1:]:
        if len(out) >= max(1, max_tables):
            break
        if len(item[6].data_rows()) >= MIN_EXTRA_ROWS:
            out.append(item[6])
    return out


def parse_poll_table(
    table: Table,
    alias_map: dict[str, str],
    *,
    max_rows: int = DEFAULT_MAX_ROWS,
    today: date | None = None,
    exclude: set[str] | None = None,
) -> list[dict[str, Any]]:
    """규칙 기반 파싱: 한 표에서 검증을 통과한 여론조사 목록을 만든다."""
    roles = classify_columns(table, alias_map, exclude)
    if roles["date"] is None or len(roles["parties"]) < 2:
        return []
    year_hint = table.context_year()
    base = table.first_data_row()
    out: list[dict[str, Any]] = []
    for offset, row in enumerate(table.data_rows()[: max(1, max_rows)]):
        row_text = table.row_text(row)
        if not row_text:
            continue
        pollster = normalize_ws(_cell(row, roles["pollster"])) or "unknown"
        if _NON_POLL_ROW.search(pollster):
            continue
        start, end = _row_dates(_cell(row, roles["date"]), year_hint, today=today)
        if end is None:
            continue
        results: dict[str, float] = {}
        for c, name in roles["parties"].items():
            if table.is_dup(base + offset, c):
                continue  # colspan으로 복제된 칸은 중복 합산을 막기 위해 건너뛴다
            v = parse_percent(_cell(row, c))
            if v is None:
                continue
            results[name] = v
        poll = {
            "pollster": pollster[:120],
            "fieldwork_start": start.isoformat() if start else None,
            "fieldwork_end": end.isoformat(),
            "sample_size": parse_int(_cell(row, roles["sample"])),
            "results": results,
            "gov_approval": parse_percent(_cell(row, roles["approval"])),
            "method": None,
            "row_text": row_text,
            "ai": False,
        }
        if validate_poll(poll, today=today):
            out.append(poll)
    return out


def _row_dates(
    cell: str, year_hint: int | None, *, today: date | None = None
) -> tuple[date | None, date | None]:
    """조사 기간 셀을 해석한다. 연도가 없으면 절 제목 연도 → 없으면 올해 기준."""
    start, end = parse_date_range(cell)
    if end is not None:
        return start, end
    if not re.search(r"\b(19|20)\d{2}\b", cell or ""):
        base = year_hint or (today or datetime.now(timezone.utc).date()).year
        start, end = parse_date_range(f"{normalize_ws(cell)} {base}")
    return start, end


def validate_poll(poll: dict[str, Any], *, today: date | None = None) -> bool:
    """CONTRACT 검증 규칙: 값 0~100, 합계 ≤ 105, 날짜 유효·미래 아님·400일 이내."""
    results = poll.get("results")
    if not isinstance(results, dict) or len(results) < 2:
        return False
    total = 0.0
    for v in results.values():
        try:
            f = float(v)
        except (TypeError, ValueError):
            return False
        if f < 0.0 or f > 100.0:
            return False
        total += f
    if total > MAX_RESULT_SUM or total <= 0.0:
        return False
    end = _as_date(poll.get("fieldwork_end"))
    if end is None:
        return False
    now = today or datetime.now(timezone.utc).date()
    if end > now:
        return False
    if (now - end).days > MAX_AGE_DAYS:
        return False
    start = _as_date(poll.get("fieldwork_start"))
    if start is not None and start > end:
        poll["fieldwork_start"] = None
    approval = poll.get("gov_approval")
    if approval is not None:
        try:
            a = float(approval)
        except (TypeError, ValueError):
            poll["gov_approval"] = None
        else:
            poll["gov_approval"] = a if 0.0 <= a <= 100.0 else None
    return True


def _as_date(value: Any) -> date | None:
    s = str(value or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return None
    try:
        return date(int(s[:4]), int(s[5:7]), int(s[8:10]))
    except ValueError:
        return None


# ---------------------------------------------------------------- LLM 보조
def _llm_table(
    iso: str, table: Table, alias_map: dict[str, str], llm: Any, ctx: Any, url: str
) -> list[dict[str, Any]]:
    """규칙 파싱이 실패한 표만 LLM으로 정규화한다 (헤더 병합·주석 대응)."""
    table_text = table.to_text(max_chars=MAX_TABLE_CHARS)
    if len(table_text) < 40:
        return []
    year = table.context_year() or datetime.now(timezone.utc).date().year
    user = (
        f"[국가코드] {iso}\n[기준 연도] {year} (표에 연도가 없으면 이 연도로 해석)\n"
        f"[표 제목] {table.heading}\n[원문 URL] {url}\n\n[표 텍스트]\n{table_text}"
    )
    try:
        raw = llm.invoke_json(
            system=_SYSTEM_PROMPT,
            user=user,
            schema=json_schema_polls,
            tool_name="emit_polls",
            max_tokens=3000,
        )
    except Exception as exc:  # noqa: BLE001 - 예산 초과/스키마 실패 격리
        record_error(ctx, SOURCE_NAME, iso, f"LLM 표 정규화 실패 {url}: {exc}")
        return []

    out: list[dict[str, Any]] = []
    for item in raw.get("polls") or []:
        if not isinstance(item, dict):
            continue
        quotes = keep_valid_quotes([item.get("row_text")], table_text, max_n=1)
        if not quotes:
            continue  # 표에 없는 행을 만들어낸 경우 → 폐기
        results: dict[str, float] = {}
        for name, v in (item.get("results") or {}).items():
            std = match_alias(str(name), alias_map) or normalize_ws(str(name))
            if _alias_key(std) in _EXCLUDE_EXACT:
                continue
            val = parse_percent(str(v))
            if val is not None:
                results[std] = val
        poll = {
            "pollster": normalize_ws(str(item.get("pollster") or "unknown"))[:120],
            "fieldwork_start": _iso_or_none(item.get("fieldwork_start")),
            "fieldwork_end": _iso_or_none(item.get("fieldwork_end")),
            "sample_size": parse_int(str(item.get("sample_size") or "")),
            "results": results,
            "gov_approval": parse_percent(str(item.get("gov_approval") or "")),
            "method": normalize_ws(str(item.get("method") or "")) or None,
            "row_text": quotes[0],
            "ai": True,
        }
        if _NON_POLL_ROW.search(poll["pollster"]):
            continue
        if validate_poll(poll):
            out.append(poll)
    return out


def _iso_or_none(value: Any) -> str | None:
    d = _as_date(value)
    return d.isoformat() if d else None


# ---------------------------------------------------------------- 문서·관측치
def _poll_doc(
    iso: str, name_ko: str, meta: dict[str, Any], poll: dict[str, Any], url: str
) -> Doc:
    ruling = str((meta.get("election") or {}).get("ruling_party_ko") or "")
    results: dict[str, float] = poll["results"]
    leader = max(results.items(), key=lambda kv: kv[1]) if results else ("", 0.0)
    parts = [f"{k} {v:g}%" for k, v in sorted(results.items(), key=lambda kv: -kv[1])[:5]]
    period = poll["fieldwork_start"] or poll["fieldwork_end"]
    sample = poll.get("sample_size")
    summary = (
        f"{name_ko} 정당 지지율 조사({poll['pollster']}, 조사기간 {period}~"
        f"{poll['fieldwork_end']}"
        + (f", 표본 {sample:,}명" if isinstance(sample, int) else "")
        + f"). 1위 {leader[0]} {leader[1]:g}%"
        + (
            f", 집권당 {ruling} {results[ruling]:g}%."
            if ruling and ruling in results
            else "."
        )
        + " 상위: "
        + ", ".join(parts)
    )
    return Doc(
        type="poll",
        iso=iso,
        date=poll["fieldwork_end"],
        id=sha12(f"{poll['pollster']}{poll['fieldwork_end']}{iso}"),
        title_ko=f"{name_ko} 여론조사 — {poll['pollster']} ({poll['fieldwork_end']})",
        summary_ko=summary,
        source_url=url,
        source_name="위키피디아",
        payload={
            "pollster": poll["pollster"],
            "fieldwork_start": poll["fieldwork_start"],
            "fieldwork_end": poll["fieldwork_end"],
            "sample_size": poll.get("sample_size"),
            "results": results,
            "gov_approval": poll.get("gov_approval"),
            "method": poll.get("method"),
        },
        ai_generated=bool(poll.get("ai")),
        model_id=poll.get("model_id"),
        confidence=0.9 if not poll.get("ai") else 0.7,
        quotes=[poll["row_text"]],
    )


def _poll_observations(
    iso: str, meta: dict[str, Any], poll: dict[str, Any], url: str, doc_id: str
) -> list[Observation]:
    election = meta.get("election") or {}
    ruling = str(election.get("ruling_party_ko") or "")
    results: dict[str, float] = poll["results"]
    flags = ["ai_generated"] if poll.get("ai") else []
    out = [
        Observation(
            indicator="party_support",
            iso=iso,
            freq="W",
            period=poll["fieldwork_end"],
            value=results.get(ruling),
            unit="%",
            source=SOURCE_NAME,
            series_id=f"{iso}:poll:{doc_id}",
            source_url=url,
            method="위키피디아 여론조사 표 규칙 파싱 (value=집권당 지지율)",
            payload={
                "results": results,
                "pollster": poll["pollster"],
                "ruling_party": ruling or None,
                "sample_size": poll.get("sample_size"),
            },
            vintage=poll["fieldwork_end"],
            flags=list(flags),
        )
    ]
    approval = poll.get("gov_approval")
    if approval is not None:
        out.append(
            Observation(
                indicator="gov_approval",
                iso=iso,
                freq="W",
                period=poll["fieldwork_end"],
                value=float(approval),
                unit="%",
                source=SOURCE_NAME,
                series_id=f"{iso}:approval:{doc_id}",
                source_url=url,
                method="위키피디아 여론조사 표의 정부 지지율 컬럼",
                payload={"pollster": poll["pollster"]},
                vintage=poll["fieldwork_end"],
                flags=list(flags),
            )
        )
    return out


def _election_doc(iso: str, name_ko: str, meta: dict[str, Any]) -> Doc | None:
    """sources.yaml의 선거 메타를 그대로 문서화한다 (사람이 관리, ai_generated=False)."""
    election = meta.get("election") or {}
    if not election:
        return None
    nxt = election.get("next_election_date")
    etype = election.get("election_type")
    payload = {
        "next_election_date": nxt,
        "election_type": etype,
        "ruling_party": election.get("ruling_party_ko"),
        "ruling_lean": election.get("ruling_lean"),
        "second_party": election.get("second_party_ko"),
        "system_note": election.get("system_note"),
        # 확장 필드: 제안서 8장 "조사 신뢰도 배지"용 (높음|보통|낮음|해당없음)
        "poll_trust": meta.get("trust"),
    }
    note = election.get("note")
    when = nxt or "미정"
    summary = f"{name_ko} 다음 전국 선거: {when} ({etype or '유형 미확인'})."
    if election.get("ruling_party_ko"):
        summary += f" 집권당 {election['ruling_party_ko']}"
        if election.get("second_party_ko"):
            summary += f", 제1야당 {election['second_party_ko']}"
        summary += "."
    if election.get("system_note"):
        summary += f" {election['system_note']}"
    if note:
        summary += f" (비고: {note})"
        payload["note"] = note
    return Doc(
        type="election",
        iso=iso,
        date=today_str(),
        id=sha12(f"election:{iso}:{when}:{etype}"),
        title_ko=f"{name_ko} 선거 일정·정당 구도",
        summary_ko=summary,
        source_url=(WIKI_BASE + str(meta["wiki_page"])) if meta.get("wiki_page") else WIKI_BASE,
        source_name="webui/macro/llm/sources.yaml (사람 관리)",
        payload=payload,
        ai_generated=False,
        confidence=1.0,
    )


def _not_applicable_doc(iso: str, name_ko: str, reason: str, meta: dict[str, Any]) -> Doc:
    """중국·사우디(경쟁 정당 없음)·유로존(정부 없음) → 정직하게 '해당 없음' 표기."""
    return Doc(
        type="not_applicable",
        iso=iso,
        date=today_str(),
        id=sha12(f"not_applicable:{iso}:{reason}"),
        title_ko=f"{name_ko} 정당 지지율 해당 없음",
        summary_ko=reason,
        source_url=str(meta.get("reference_url") or WIKI_BASE),
        source_name="webui/macro/llm/sources.yaml (사람 관리)",
        payload={"reason": reason},
        ai_generated=False,
        confidence=1.0,
    )
