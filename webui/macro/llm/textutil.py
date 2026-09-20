# ============================================================
# [모듈 개요] 정성 소스 공용 텍스트·HTML 유틸리티
#
# LLM 계층(webui/macro/llm/*)이 공유하는 순수 함수 모음입니다. 외부 의존성 없이
# 표준 라이브러리(html.parser)만 사용합니다 — 워커 이미지에 lxml/bs4가 없을 수
# 있으므로 pandas.read_html·BeautifulSoup에 의존하지 않습니다.
#
# 제공 기능:
#  - html_to_text(html): script/style 제거 → 엔티티 복원 → 공백 정리
#  - normalize_ws(s): 유니코드 공백·줄바꿈을 단일 스페이스로 축약
#  - quote_in_text(quote, text): 공백·따옴표·대시 정규화 후 부분 문자열 일치
#    (LLM이 만든 인용문이 원문에 실제로 존재하는지 검증하는 유일한 관문)
#  - parse_date_range(s): 영문 월명·범위 표기("12–14 Sep 2026",
#    "September 12–14, 2026", "29 Aug – 2 Sep 2026", ISO 등) → (start, end) date
#  - extract_tables(html): rowspan/colspan을 펼친 표 격자 (위키 여론조사 표용)
#  - extract_links(html): (href, text) 목록 (중앙은행 결정문 링크 탐색용)
#  - sha12(s): sha1 앞 12자리 hex — 문서 id 생성 규칙 (CONTRACT 4장)
# ============================================================
from __future__ import annotations

import calendar
import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from html.parser import HTMLParser

__all__ = [
    "Table",
    "extract_links",
    "extract_tables",
    "html_to_text",
    "normalize_ws",
    "parse_date_range",
    "parse_int",
    "parse_percent",
    "quote_in_text",
    "sha12",
]

# 본문에 포함되면 안 되는 태그 (내용까지 버린다)
_DROP_TAGS = {"script", "style", "noscript", "template", "svg", "head"}
# 사이트 내비게이션·푸터·검색폼은 LLM 토큰만 잡아먹으므로 버린다
_CHROME_TAGS = {"nav", "footer", "form", "aside"}
# 본문 영역으로 볼 수 있는 표지 (id/class/role) — 있으면 그 안만 쓴다
_MAIN_TAGS = {"main", "article"}
_MAIN_IDS = {
    "article", "content", "main", "main-content", "maincontent", "primary",
    "contentarea", "page-content", "press-release", "bodycontent",
}
_MAIN_CLASS_HINTS = ("article-body", "entry-content", "post-content", "press-release", "mw-parser-output")
# 블록 태그는 앞뒤로 줄바꿈을 넣어 문장이 붙지 않게 한다
_BLOCK_TAGS = {
    "p", "div", "br", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6",
    "section", "article", "table", "ul", "ol", "blockquote", "header", "footer", "hr",
}

_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

_WS_RE = re.compile(r"[\s  -​　﻿]+")
_MULTI_NL_RE = re.compile(r"\n{3,}")


def sha12(s: str) -> str:
    """문자열의 sha1 앞 12자리 hex — 같은 입력이면 항상 같은 문서 id."""
    return hashlib.sha1(s.encode("utf-8", "replace")).hexdigest()[:12]


def normalize_ws(s: str) -> str:
    """모든 종류의 공백(개행·NBSP·전각 스페이스 포함)을 단일 스페이스로 축약."""
    if not s:
        return ""
    return _WS_RE.sub(" ", s).strip()


def _is_main_marker(tag: str, attrs: dict[str, str]) -> bool:
    """이 요소가 '본문 영역'인지 판정한다 (main/article/role=main/id=article ...)."""
    if tag in _MAIN_TAGS:
        return True
    if (attrs.get("role") or "").strip().lower() == "main":
        return True
    if (attrs.get("id") or "").strip().lower().replace("_", "-") in _MAIN_IDS:
        return True
    cls = (attrs.get("class") or "").lower()
    return any(h in cls for h in _MAIN_CLASS_HINTS)


class _TextExtractor(HTMLParser):
    """HTML → 평문. script/style/nav/footer 내용은 버리고 블록 경계에 개행을 넣는다.

    본문 영역(main/article/#article ...)이 있으면 그 안의 텍스트를 따로 모아
    두고, 충분히 길면 그것만 반환한다 — 사이트 메뉴가 LLM 입력의 대부분을
    차지하는 문제(연준 페이지 실측: 11k자 중 본문 3k자)를 막는다.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.main_parts: list[str] = []
        self._drop_depth = 0
        self._main_tag: str | None = None
        self._main_level = 0

    def _emit(self, text: str) -> None:
        self.parts.append(text)
        if self._main_tag is not None:
            self.main_parts.append(text)

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        if tag in _DROP_TAGS or tag in _CHROME_TAGS:
            self._drop_depth += 1
            return
        if self._main_tag is not None and tag == self._main_tag:
            self._main_level += 1
        elif self._main_tag is None and self._drop_depth == 0:
            attrs = {k: (v or "") for k, v in attrs_list}
            if _is_main_marker(tag, attrs):
                self._main_tag = tag
                self._main_level = 1
        if tag in _BLOCK_TAGS:
            self._emit("\n")

    def handle_startendtag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_TAGS:
            self._emit("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_TAGS or tag in _CHROME_TAGS:
            self._drop_depth = max(0, self._drop_depth - 1)
            return
        if tag in _BLOCK_TAGS:
            self._emit("\n")
        if self._main_tag is not None and tag == self._main_tag:
            self._main_level -= 1
            if self._main_level <= 0:
                self._main_tag = None

    def handle_data(self, data: str) -> None:
        if self._drop_depth == 0 and data:
            self._emit(data)


def html_to_text(html: str, *, max_chars: int | None = None) -> str:
    """HTML 문서를 사람이 읽을 수 있는 평문으로 변환한다.

    스크립트/스타일/내비게이션/푸터를 제거하고, 본문 영역(main·article·#article
    ...)이 400자 이상이면 그 영역만 쓴다. 블록 태그 경계에서 줄을 나눈 뒤 줄
    안의 공백은 한 칸으로 줄이고 빈 줄은 최대 1개로 정리한다. max_chars가
    주어지면 앞에서 자른다(LLM 입력 상한용).
    """
    if not html:
        return ""
    p = _TextExtractor()
    try:
        p.feed(html)
        p.close()
    except Exception:  # noqa: BLE001 - 깨진 HTML도 최대한 살린다 (fail-open)
        pass
    main = "".join(p.main_parts)
    raw = main if len(normalize_ws(main)) >= 400 else "".join(p.parts)
    lines = [normalize_ws(ln) for ln in raw.split("\n")]
    text = _MULTI_NL_RE.sub("\n\n", "\n".join(lines)).strip()
    if max_chars is not None and len(text) > max_chars:
        text = text[:max_chars]
    return text


# 인용 검증용 정규화: 따옴표·대시·괄호 각주를 통일해 사소한 차이로 실패하지 않게 한다
_QUOTE_CHARS = dict.fromkeys(map(ord, "‘’‚‛′´`"), "'")
_QUOTE_CHARS.update(dict.fromkeys(map(ord, "“”„‟″«»"), '"'))
_DASH_CHARS = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")
_FOOTNOTE_RE = re.compile(r"\[[0-9a-zA-Z]{1,3}\]")


def _norm_for_match(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    s = s.translate(_QUOTE_CHARS).translate(_DASH_CHARS)
    s = _FOOTNOTE_RE.sub("", s)
    return normalize_ws(s).lower()


def quote_in_text(quote: str, text: str) -> bool:
    """LLM이 제시한 인용문이 원문 text 안에 부분 문자열로 존재하는지 검사한다.

    공백·따옴표·대시·각주 표기를 정규화하고 대소문자를 무시한 뒤 부분 일치를
    본다. 너무 짧은 인용(8자 미만)은 우연 일치가 가능하므로 거부한다.
    """
    q = _norm_for_match(quote)
    if len(q) < 8:
        return False
    return q in _norm_for_match(text)


# ---------------------------------------------------------------- 날짜 파싱
_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_MON = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_DASH = r"\s*(?:-|to|until|~)\s*"

# 뒤에 붙는 시각(2026-09-16T18:30:00Z)까지 허용해야 RSS/Atom 날짜를 읽을 수 있다
_ISO_RE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})(?![\d-])")
# "29 Aug - 2 Sep 2026" / "30 Dec 2025 - 2 Jan 2026"
_DMY_RANGE_RE = re.compile(
    rf"\b(\d{{1,2}})\s+({_MON})(?:\s+(\d{{4}}))?{_DASH}(\d{{1,2}})\s+({_MON})\s+(\d{{4}})\b",
    re.I,
)
# "12-14 Sep 2026"
_D_RANGE_DMY_RE = re.compile(rf"\b(\d{{1,2}}){_DASH}(\d{{1,2}})\s+({_MON})\s+(\d{{4}})\b", re.I)
# "Sep 12 - Oct 2, 2026"
_MDY_RANGE_RE = re.compile(
    rf"\b({_MON})\s+(\d{{1,2}})(?:,?\s+(\d{{4}}))?{_DASH}({_MON})\s+(\d{{1,2}}),?\s+(\d{{4}})\b",
    re.I,
)
# "September 12-14, 2026"
_MD_RANGE_Y_RE = re.compile(
    rf"\b({_MON})\s+(\d{{1,2}}){_DASH}(\d{{1,2}}),?\s+(\d{{4}})\b", re.I
)
# "12 Sep 2026"
_DMY_RE = re.compile(rf"\b(\d{{1,2}})\s+({_MON}),?\s+(\d{{4}})\b", re.I)
# "September 12, 2026"
_MDY_RE = re.compile(rf"\b({_MON})\s+(\d{{1,2}}),?\s+(\d{{4}})\b", re.I)
# "Sep 2026" (월 단위 → 그 달 1일~말일)
_MY_RE = re.compile(rf"\b({_MON}),?\s+(\d{{4}})\b", re.I)
# "12/09/2026" 계열
_SLASH_RE = re.compile(r"\b(\d{1,4})[/.](\d{1,2})[/.](\d{2,4})\b")


def _mon(tok: str) -> int | None:
    return _MONTHS.get(tok.strip().rstrip(".").lower())


def _mk(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse_date_range(s: str) -> tuple[date | None, date | None]:
    """여론조사 표의 '조사 기간' 문자열을 (시작일, 종료일)로 해석한다.

    지원 형식(위키피디아 여론조사 표에서 실제로 쓰이는 것들):
      "2026-09-12" · "2026-09-12 – 2026-09-14" · "12 Sep 2026" ·
      "12–14 Sep 2026" · "29 Aug – 2 Sep 2026" · "30 Dec 2025 – 2 Jan 2026" ·
      "September 12, 2026" · "September 12–14, 2026" · "Sep 12 – Oct 2, 2026" ·
      "Sep 2026"(→ 1일~말일) · "12/09/2026"(일-월-년 우선)
    해석할 수 없으면 (None, None). 단일 날짜면 시작=종료.
    """
    if not s:
        return (None, None)
    t = _FOOTNOTE_RE.sub(" ", unicodedata.normalize("NFKC", str(s)))
    t = normalize_ws(t.translate(_DASH_CHARS))
    if not t:
        return (None, None)

    iso = _ISO_RE.findall(t)
    if iso:
        first = _mk(int(iso[0][0]), int(iso[0][1]), int(iso[0][2]))
        last = _mk(int(iso[-1][0]), int(iso[-1][1]), int(iso[-1][2])) if len(iso) > 1 else first
        if first and last:
            return (min(first, last), max(first, last))

    m = _DMY_RANGE_RE.search(t)
    if m:
        d1, mo1, y1, d2, mo2, y2 = m.groups()
        mm1, mm2 = _mon(mo1), _mon(mo2)
        year2 = int(y2)
        year1 = int(y1) if y1 else year2
        if mm1 and mm2:
            a, b = _mk(year1, mm1, int(d1)), _mk(year2, mm2, int(d2))
            return (min(a, b), max(a, b)) if a and b else (None, None)

    m = _MDY_RANGE_RE.search(t)
    if m:
        mo1, d1, y1, mo2, d2, y2 = m.groups()
        mm1, mm2 = _mon(mo1), _mon(mo2)
        year2 = int(y2)
        year1 = int(y1) if y1 else year2
        if mm1 and mm2:
            a, b = _mk(year1, mm1, int(d1)), _mk(year2, mm2, int(d2))
            return (min(a, b), max(a, b)) if a and b else (None, None)

    m = _D_RANGE_DMY_RE.search(t)
    if m:
        d1, d2, mo, y = m.groups()
        mm = _mon(mo)
        if mm:
            a, b = _mk(int(y), mm, int(d1)), _mk(int(y), mm, int(d2))
            return (min(a, b), max(a, b)) if a and b else (None, None)

    m = _MD_RANGE_Y_RE.search(t)
    if m:
        mo, d1, d2, y = m.groups()
        mm = _mon(mo)
        if mm:
            a, b = _mk(int(y), mm, int(d1)), _mk(int(y), mm, int(d2))
            return (min(a, b), max(a, b)) if a and b else (None, None)

    m = _DMY_RE.search(t)
    if m:
        d, mo, y = m.groups()
        mm = _mon(mo)
        if mm:
            one = _mk(int(y), mm, int(d))
            return (one, one) if one else (None, None)

    m = _MDY_RE.search(t)
    if m:
        mo, d, y = m.groups()
        mm = _mon(mo)
        if mm:
            one = _mk(int(y), mm, int(d))
            return (one, one) if one else (None, None)

    m = _SLASH_RE.search(t)
    if m:
        a, b, c = (int(x) for x in m.groups())
        cand: date | None = None
        if m.group(1) and len(m.group(1)) == 4:  # YYYY/MM/DD
            cand = _mk(a, b, c)
        else:
            year = c if c > 99 else 2000 + c
            if a > 12 and b <= 12:  # 일-월-년
                cand = _mk(year, b, a)
            elif b > 12 and a <= 12:  # 월-일-년
                cand = _mk(year, a, b)
            else:  # 모호하면 위키 표기 관례인 일-월-년
                cand = _mk(year, b, a)
        if cand:
            return (cand, cand)

    m = _MY_RE.search(t)
    if m:
        mo, y = m.groups()
        mm = _mon(mo)
        if mm:
            y_i = int(y)
            a = _mk(y_i, mm, 1)
            b = _mk(y_i, mm, calendar.monthrange(y_i, mm)[1])
            if a and b:
                return (a, b)

    return (None, None)


_PCT_RE = re.compile(r"-?\d{1,3}(?:[.,]\d{1,2}(?!\d))?")
_INT_RE = re.compile(r"\d[\d,. ]*")


def parse_percent(cell: str) -> float | None:
    """표 셀에서 백분율 숫자를 뽑는다. '39.5%', '39,5', '<1', '—' 등을 처리."""
    if cell is None:
        return None
    t = _FOOTNOTE_RE.sub("", normalize_ws(str(cell)).translate(_DASH_CHARS))
    t = t.replace("%", "").replace("–", "-").strip()
    if not t or t in {"-", "--", "?", "n/a", "na", "tbd", "—"}:
        return None
    if t.startswith("<"):
        t = t[1:].strip()
    m = _PCT_RE.search(t)
    if not m:
        return None
    num = m.group(0)
    # 유럽식 소수 쉼표(39,5)는 소수점으로, 천단위 쉼표(1,234)는 제거
    if "," in num:
        head, _, tail = num.partition(",")
        num = f"{head}.{tail}" if len(tail) <= 2 else head + tail
    try:
        return float(num)
    except ValueError:
        return None


def parse_int(cell: str) -> int | None:
    """표 셀에서 표본 수 같은 정수를 뽑는다. '1,234', '1 234', '≈2.000' 등을 처리."""
    if cell is None:
        return None
    t = _FOOTNOTE_RE.sub("", normalize_ws(str(cell)))
    m = _INT_RE.search(t)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(0))
    if not digits or len(digits) > 9:
        return None
    return int(digits)


# ---------------------------------------------------------------- 표/링크 파싱
@dataclass
class Table:
    """rowspan/colspan을 펼친 표. rows[r][c] 는 항상 문자열(빈 셀은 "").

    heading은 이 표 바로 앞의 h1~h6 제목, caption은 <caption> 내용이다.
    위키 여론조사 표는 연도를 절('2026' 등)로 나누고 날짜 칸에는 연도를 빼는
    경우가 많아, 연도 보정에 heading/caption이 반드시 필요하다.
    """

    attrs: dict[str, str] = field(default_factory=dict)
    rows: list[list[str]] = field(default_factory=list)
    tags: list[list[str]] = field(default_factory=list)
    dups: list[list[bool]] = field(default_factory=list)
    heading: str = ""
    caption: str = ""

    def context_year(self) -> int | None:
        """heading/caption에서 4자리 연도를 찾는다 (없으면 None)."""
        for src in (self.caption, self.heading):
            for m in re.finditer(r"\b(19|20)\d{2}\b", src or ""):
                y = int(m.group(0))
                if 1900 <= y <= 2100:
                    return y
        return None

    @property
    def classes(self) -> list[str]:
        return (self.attrs.get("class") or "").split()

    @property
    def n_cols(self) -> int:
        return max((len(r) for r in self.rows), default=0)

    def header_rows(self) -> list[list[str]]:
        """앞쪽 연속된 th 전용 행들 = 헤더 (위키 표는 2행 헤더가 흔하다)."""
        out: list[list[str]] = []
        for r, tagrow in enumerate(self.tags):
            if tagrow and all(t == "th" for t in tagrow if t):
                out.append(self.rows[r])
            else:
                break
            if r >= 3:  # 헤더가 4행을 넘는 경우는 없다고 본다
                break
        return out

    def headers(self) -> list[str]:
        """헤더 행들을 열 단위로 합친 문자열 목록."""
        hrows = self.header_rows()
        if not hrows:
            return []
        width = max(len(r) for r in hrows)
        out = []
        for c in range(width):
            seen: list[str] = []
            for r in hrows:
                v = r[c] if c < len(r) else ""
                if v and v not in seen:
                    seen.append(v)
            out.append(normalize_ws(" ".join(seen)))
        return out

    def data_rows(self) -> list[list[str]]:
        return self.rows[len(self.header_rows()):]

    def first_data_row(self) -> int:
        """data_rows()[0]의 절대 행 인덱스 (is_dup 조회에 필요)."""
        return len(self.header_rows())

    def is_dup(self, row_index: int, col: int) -> bool:
        """colspan/rowspan 때문에 같은 값이 복제된 칸인지 여부.

        호주 표 실측: LIB/NAT를 나누지 않는 조사기관의 행은 L/NP 값 하나가
        colspan=2로 들어와 두 칸에 같은 숫자가 복제된다. 그대로 합산하면
        지지율 합이 120%가 되어 유효한 조사가 전부 버려진다.
        """
        if 0 <= row_index < len(self.dups) and 0 <= col < len(self.dups[row_index]):
            return self.dups[row_index][col]
        return False

    def to_text(self, *, max_chars: int | None = None) -> str:
        lines = [" | ".join(r) for r in self.rows]
        txt = "\n".join(lines)
        if max_chars is not None and len(txt) > max_chars:
            txt = txt[:max_chars]
        return txt

    def row_text(self, row: list[str]) -> str:
        return normalize_ws(" | ".join(x for x in row if x))


class _TableParser(HTMLParser):
    """<table>을 셀 단위로 수집한다. 중첩 표는 스택으로 분리해 각각 반환한다."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[Table] = []
        # 스택 원소: {"attrs":..., "rows": [[(text, colspan, rowspan, tag)]]}
        self._stack: list[dict] = []
        self._cell: list[str] | None = None
        self._cell_meta: tuple[int, int, str] | None = None
        self._drop_depth = 0
        self._sup_depth = 0
        self._last_heading = ""
        self._heading_buf: list[str] | None = None
        self._caption_buf: list[str] | None = None

    # --- helpers
    @staticmethod
    def _span(attrs: dict[str, str], key: str) -> int:
        try:
            v = int(re.sub(r"\D", "", attrs.get(key, "1") or "1") or "1")
        except ValueError:
            v = 1
        return max(1, min(v, 40))

    def _close_cell(self) -> None:
        if self._cell is None or not self._stack:
            self._cell = None
            self._cell_meta = None
            return
        text = normalize_ws("".join(self._cell))
        cs, rs, tag = self._cell_meta or (1, 1, "td")
        rows = self._stack[-1]["rows"]
        if not rows:
            rows.append([])
        rows[-1].append((text, cs, rs, tag))
        self._cell = None
        self._cell_meta = None

    # --- HTMLParser hooks
    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = {k: (v or "") for k, v in attrs_list}
        if tag in _DROP_TAGS:
            self._drop_depth += 1
            return
        if tag == "sup":  # 위키 각주 [1] 는 셀 내용에서 제외
            self._sup_depth += 1
            return
        if tag in _HEADING_TAGS:
            self._heading_buf = []
            return
        if tag == "table":
            self._stack.append(
                {"attrs": attrs, "rows": [], "heading": self._last_heading, "caption": ""}
            )
            return
        if not self._stack:
            return
        if tag == "caption":
            self._caption_buf = []
            return
        if tag == "tr":
            self._close_cell()
            self._stack[-1]["rows"].append([])
            return
        if tag in ("td", "th"):
            self._close_cell()
            self._cell = []
            self._cell_meta = (self._span(attrs, "colspan"), self._span(attrs, "rowspan"), tag)
            return
        if tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_startendtag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        if tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_TAGS:
            self._drop_depth = max(0, self._drop_depth - 1)
            return
        if tag == "sup":
            self._sup_depth = max(0, self._sup_depth - 1)
            return
        if tag in _HEADING_TAGS:
            if self._heading_buf is not None:
                self._last_heading = normalize_ws("".join(self._heading_buf))
            self._heading_buf = None
            return
        if tag == "caption":
            if self._caption_buf is not None and self._stack:
                self._stack[-1]["caption"] = normalize_ws("".join(self._caption_buf))
            self._caption_buf = None
            return
        if tag in ("td", "th"):
            self._close_cell()
            return
        if tag == "tr":
            self._close_cell()
            return
        if tag == "table" and self._stack:
            self._close_cell()
            t = self._stack.pop()
            self.tables.append(
                _expand(t["attrs"], t["rows"], t.get("heading", ""), t.get("caption", ""))
            )

    def handle_data(self, data: str) -> None:
        if self._drop_depth or self._sup_depth:
            return
        if self._heading_buf is not None and data:
            self._heading_buf.append(data)
            return
        if self._caption_buf is not None and data:
            self._caption_buf.append(data)
            return
        if self._cell is not None and data:
            self._cell.append(data)

    def close(self) -> None:  # 닫히지 않은 <table>도 회수한다
        super().close()
        while self._stack:
            self._close_cell()
            t = self._stack.pop()
            self.tables.append(
                _expand(t["attrs"], t["rows"], t.get("heading", ""), t.get("caption", ""))
            )


def _expand(
    attrs: dict[str, str],
    rows: list[list[tuple[str, int, int, str]]],
    heading: str = "",
    caption: str = "",
) -> Table:
    """(text, colspan, rowspan) 셀 목록을 직사각형 격자로 펼친다."""
    rows = [r for r in rows if r]
    cellmap: list[dict[int, tuple[str, str, bool]]] = [{} for _ in rows]
    for r, row in enumerate(rows):
        c = 0
        for text, cs, rs, tag in row:
            while c in cellmap[r]:
                c += 1
            for dr in range(rs):
                if r + dr >= len(cellmap):
                    break
                for dc in range(cs):
                    dup = dr > 0 or dc > 0  # 원본 칸이 아니라 span으로 복제된 칸
                    cellmap[r + dr].setdefault(c + dc, (text, tag, dup))
            c += cs
    width = max((max(m) + 1 if m else 0) for m in cellmap) if cellmap else 0
    empty = ("", "", False)
    grid, tags, dups = [], [], []
    for m in cellmap:
        grid.append([m.get(i, empty)[0] for i in range(width)])
        tags.append([m.get(i, empty)[1] for i in range(width)])
        dups.append([m.get(i, empty)[2] for i in range(width)])
    return Table(
        attrs=attrs, rows=grid, tags=tags, dups=dups, heading=heading, caption=caption
    )


def extract_tables(html: str, *, css_class: str | None = None) -> list[Table]:
    """HTML의 모든 <table>을 파싱한다. css_class를 주면 그 클래스만 반환."""
    if not html:
        return []
    p = _TableParser()
    try:
        p.feed(html)
        p.close()
    except Exception:  # noqa: BLE001 - 깨진 HTML도 수집한 만큼 쓴다
        pass
    tables = [t for t in p.tables if t.rows]
    if css_class:
        tables = [t for t in tables if css_class in t.classes]
    return tables


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._buf: list[str] = []
        self._drop_depth = 0

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        if tag in _DROP_TAGS:
            self._drop_depth += 1
            return
        if tag == "a":
            if self._href is not None:  # 중첩 <a> — 앞의 것을 닫는다
                self._flush()
            attrs = {k: (v or "") for k, v in attrs_list}
            self._href = attrs.get("href", "")
            self._buf = []

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP_TAGS:
            self._drop_depth = max(0, self._drop_depth - 1)
            return
        if tag == "a":
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._drop_depth == 0 and self._href is not None:
            self._buf.append(data)

    def _flush(self) -> None:
        if self._href is not None:
            self.links.append((self._href.strip(), normalize_ws("".join(self._buf))))
        self._href = None
        self._buf = []

    def close(self) -> None:
        super().close()
        self._flush()


def extract_links(html: str, base_url: str | None = None) -> list[tuple[str, str]]:
    """(절대 href, 앵커 텍스트) 목록. base_url을 주면 상대경로를 해석한다."""
    if not html:
        return []
    p = _LinkParser()
    try:
        p.feed(html)
        p.close()
    except Exception:  # noqa: BLE001
        pass
    out: list[tuple[str, str]] = []
    for href, text in p.links:
        if not href or href.startswith(("#", "mailto:")) or is_blocked_scheme(href):
            continue
        if base_url:
            from urllib.parse import urljoin

            href = urljoin(base_url, href)
        if is_blocked_scheme(href):
            continue
        out.append((href, text))
    return out


# 링크 후보에서 절대 따라가지 않는 스킴 (문서 source_url·payload.sources로 프론트에 노출되므로
# `javascript:`/`data:`/`vbscript:`는 원문 수집 단계에서 걸러낸다. 공백·대소문자 변형 포함)
_BLOCKED_SCHEME_RE = re.compile(r"^\s*(javascript|data|vbscript|file)\s*:", re.I)


def is_blocked_scheme(url: str) -> bool:
    """실행 가능한 스킴(javascript:/data:/vbscript:/file:)이면 True."""
    return bool(_BLOCKED_SCHEME_RE.match(str(url or "")))
