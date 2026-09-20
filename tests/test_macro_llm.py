# ============================================================
# [모듈 개요] 정성 데이터·LLM 계층(webui/macro/llm) 단위 테스트
#
# 네트워크·Bedrock 실호출은 전혀 하지 않는다:
#  - HTTP는 StubCtx(픽스처 HTML을 URL로 매핑)로 대체
#  - LLM은 StubLLM(미리 정해둔 dict 반환)으로 대체
#  - BedrockJson은 가짜 client.converse 객체를 주입해 프로토콜만 검증
#
# 픽스처(tests/fixtures/macro/llm/)는 실제 페이지에서 잘라온 축소본이다:
#  wiki_polls_{kr,de,gb}.html — 영문 위키피디아 여론조사 표(첫 표 + 6행)
#  cb_statement_us.html       — federalreserve.gov FOMC 성명 본문 div
# ============================================================
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "webui"))

from macro.llm import (  # noqa: E402
    bedrock_json as bj,
    cb_statements as cb,
    common,
    energy_policy as ep,
    polls_wiki as pw,
    textutil as tu,
    weekly_brief as wb,
)
from macro.llm.meta import load_sources  # noqa: E402

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "macro" / "llm"
TODAY = date(2026, 9, 20)  # 픽스처 기준일 (표의 최신 조사가 2026-09 중순)


# ---------------------------------------------------------------- 스텁
@dataclass
class Country:
    """registry의 Country 덕 타이핑 대역 (iso, name_ko, euro만 쓴다)."""

    iso: str
    name_ko: str
    euro: str = "no"


class StubResponse:
    def __init__(self, text: str, status: int = 200) -> None:
        self.text = text
        self.status_code = status
        self.content = text.encode("utf-8")


class StubCtx:
    """CollectContext 대역. sources/base.py 없이도 테스트가 돌아가야 한다."""

    def __init__(self, pages=None, *, no_llm=False, llm=None, extra=None, fail=()):
        self.pages = dict(pages or {})
        self.no_llm = no_llm
        self.llm = llm
        self.extra = dict(extra or {})
        self.dry_run = True
        self.since = None
        self.errors: list[str] = []
        self.logs: list[str] = []
        self.raw: list[tuple[str, str, int]] = []
        self.requested: list[str] = []
        self.fail = set(fail)

    def get(self, url, params=None, headers=None, timeout=30):
        self.requested.append(url)
        if url in self.fail:
            raise RuntimeError("연결 실패(스텁)")
        if url not in self.pages:
            return StubResponse("", status=404)
        return StubResponse(self.pages[url])

    def save_raw(self, source, name, data):
        self.raw.append((source, name, len(data)))

    def record_error(self, source, iso, msg):
        self.errors.append(f"{source}:{iso}: {msg}")

    def log(self, msg):
        self.logs.append(str(msg))


class StubLLM:
    """invoke_json 호출을 기록하고 미리 정한 응답을 순서대로 돌려준다."""

    def __init__(self, responses, model_id="stub-model-1"):
        self.responses = list(responses) if isinstance(responses, list) else [responses]
        self.model_id = model_id
        self.calls: list[dict] = []

    def invoke_json(self, *, system, user, schema, tool_name="emit", max_tokens=2000, **kw):
        self.calls.append(
            {"system": system, "user": user, "schema": schema, "tool_name": tool_name}
        )
        if not self.responses:
            raise AssertionError("StubLLM 응답이 소진되었다")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ================================================================ textutil
class TestTextUtil:
    @pytest.mark.parametrize(
        ("raw", "start", "end"),
        [
            ("2026-09-12", date(2026, 9, 12), date(2026, 9, 12)),
            ("2026-09-16T18:30:00Z", date(2026, 9, 16), date(2026, 9, 16)),
            ("Thu, 10 Sep 2026 14:15:00 +0200", date(2026, 9, 10), date(2026, 9, 10)),
            ("2026-09-12 – 2026-09-14", date(2026, 9, 12), date(2026, 9, 14)),
            ("12 Sep 2026", date(2026, 9, 12), date(2026, 9, 12)),
            ("12–14 Sep 2026", date(2026, 9, 12), date(2026, 9, 14)),
            ("12-14 September 2026", date(2026, 9, 12), date(2026, 9, 14)),
            ("29 Aug – 2 Sep 2026", date(2026, 8, 29), date(2026, 9, 2)),
            ("30 Dec 2025 – 2 Jan 2026", date(2025, 12, 30), date(2026, 1, 2)),
            ("September 12–14, 2026", date(2026, 9, 12), date(2026, 9, 14)),
            ("Sept 12, 2026", date(2026, 9, 12), date(2026, 9, 12)),
            ("Sep 12 – Oct 2, 2026", date(2026, 9, 12), date(2026, 10, 2)),
            ("Sep 2026", date(2026, 9, 1), date(2026, 9, 30)),
            ("12/09/2026", date(2026, 9, 12), date(2026, 9, 12)),
            ("2026/09/12", date(2026, 9, 12), date(2026, 9, 12)),
            ("8–10 Sep[a]", None, None),  # 연도 없음 → 호출자가 보정
            ("", None, None),
            ("소문난 잔치", None, None),
        ],
    )
    def test_parse_date_range(self, raw, start, end):
        assert tu.parse_date_range(raw) == (start, end)

    def test_parse_date_range_swaps_reversed_range(self):
        assert tu.parse_date_range("14–12 Sep 2026") == (date(2026, 9, 12), date(2026, 9, 14))

    def test_parse_date_range_rejects_impossible_day(self):
        assert tu.parse_date_range("31 Feb 2026") == (None, None)

    def test_quote_in_text_normalizes_whitespace_and_quotes(self):
        text = "The Committee decided to\n  lower the target   range by “1/2” point."
        assert tu.quote_in_text("lower the target range by \"1/2\" point", text)
        assert tu.quote_in_text("The Committee decided to lower the target range", text)

    def test_quote_in_text_rejects_absent_and_too_short(self):
        text = "Inflation remains somewhat elevated."
        assert not tu.quote_in_text("the Committee raised rates by 50bp", text)
        assert not tu.quote_in_text("remains", text)  # 8자 미만은 우연 일치 방지로 거부

    def test_normalize_ws_and_sha12(self):
        assert tu.normalize_ws(" a  b\n\tc ") == "a b c"
        assert tu.sha12("x") == tu.sha12("x")
        assert len(tu.sha12("x")) == 12
        assert tu.sha12("a") != tu.sha12("b")

    def test_extract_tables_expands_rowspan_colspan(self):
        html = """
        <table class="wikitable">
          <caption>2026 test</caption>
          <tr><th rowspan="2">Pollster</th><th colspan="2">Parties</th></tr>
          <tr><th>A</th><th>B</th></tr>
          <tr><td>Alpha</td><td>40</td><td>30</td></tr>
        </table>"""
        (t,) = tu.extract_tables(html, css_class="wikitable")
        assert t.headers() == ["Pollster", "Parties A", "Parties B"]
        assert t.data_rows() == [["Alpha", "40", "30"]]
        assert t.context_year() == 2026

    def test_extract_tables_drops_footnote_sup(self):
        html = '<table class="wikitable"><tr><th>P</th></tr><tr><td>X<sup>[1]</sup></td></tr></table>'
        (t,) = tu.extract_tables(html)
        assert t.data_rows() == [["X"]]

    def test_html_to_text_prefers_main_and_drops_chrome(self):
        html = (
            "<html><body><nav>메뉴 메뉴 메뉴</nav>"
            "<div id='article'><p>" + "본문 문장입니다. " * 40 + "</p></div>"
            "<footer>푸터</footer><script>var x=1;</script></body></html>"
        )
        text = tu.html_to_text(html)
        assert "메뉴" not in text and "푸터" not in text and "var x" not in text
        assert text.startswith("본문 문장입니다.")

    def test_html_to_text_falls_back_when_main_is_short(self):
        html = "<html><body><main>짧다</main><p>이 문장은 본문 밖에 있지만 남아야 한다</p></body></html>"
        assert "본문 밖에" in tu.html_to_text(html)

    def test_html_to_text_max_chars(self):
        assert len(tu.html_to_text("<p>" + "가" * 500 + "</p>", max_chars=100)) == 100

    def test_parse_percent_and_int(self):
        assert tu.parse_percent("39.5%") == 39.5
        assert tu.parse_percent("39,5") == 39.5  # 유럽식 소수 쉼표
        assert tu.parse_percent("<1") == 1.0
        assert tu.parse_percent("—N/a") is None
        assert tu.parse_percent("banned") is None
        assert tu.parse_int("1,002") == 1002
        assert tu.parse_int("—") is None

    def test_extract_links_resolves_relative(self):
        html = '<a href="/a/b.htm">One</a><a href="#x">skip</a><a href="javascript:void">no</a>'
        assert tu.extract_links(html, "https://e.org/c/d.htm") == [
            ("https://e.org/a/b.htm", "One")
        ]

    def test_extract_links_drops_executable_schemes(self):
        html = (
            '<a href=" JavaScript:alert(1)">a</a><a href="data:text/html;base64,PHNjcmlwdD4=">b</a>'
            '<a href="VBScript:msgbox">c</a><a href="file:///etc/passwd">d</a>'
            '<a href="https://ok.example/x">ok</a><a href="//cdn.example/y">proto-rel</a>'
        )
        assert tu.extract_links(html, "https://e.org/") == [
            ("https://ok.example/x", "ok"),
            ("https://cdn.example/y", "proto-rel"),
        ]
        assert tu.is_blocked_scheme("javascript:x") and tu.is_blocked_scheme("  data:,x")
        assert not tu.is_blocked_scheme("https://a") and not tu.is_blocked_scheme("/rel")


# ================================================================ BedrockJson
def converse_ok(payload, *, name="emit", tokens=(120, 80)):
    return {
        "output": {"message": {"content": [{"toolUse": {"name": name, "input": payload}}]}},
        "usage": {"inputTokens": tokens[0], "outputTokens": tokens[1]},
        "stopReason": "tool_use",
    }


class FakeThrottling(Exception):
    def __init__(self):
        super().__init__("Too many requests")
        self.response = {"Error": {"Code": "ThrottlingException"}}


class FakeClient:
    def __init__(self, results):
        self.results = list(results)
        self.calls: list[dict] = []

    def converse(self, **params):
        self.calls.append(params)
        item = self.results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


SIMPLE_SCHEMA = {
    "type": "object",
    "properties": {"a": {"type": "string"}, "n": {"type": "number"}},
    "required": ["a"],
}


class TestBedrockJson:
    def test_returns_tool_use_input_and_forces_tool_choice(self):
        client = FakeClient([converse_ok({"a": "ok", "n": 1}, name="emit_stance")])
        llm = bj.BedrockJson(client=client, model_id="m1")
        out = llm.invoke_json(
            system="s", user="u", schema=SIMPLE_SCHEMA, tool_name="emit_stance", max_tokens=99
        )
        assert out == {"a": "ok", "n": 1}
        (params,) = client.calls
        assert params["modelId"] == "m1"
        assert params["system"] == [{"text": "s"}]
        assert params["messages"] == [{"role": "user", "content": [{"text": "u"}]}]
        assert params["inferenceConfig"] == {"maxTokens": 99, "temperature": 0.0}
        spec = params["toolConfig"]["tools"][0]["toolSpec"]
        assert spec["name"] == "emit_stance"
        assert spec["inputSchema"] == {"json": SIMPLE_SCHEMA}
        assert params["toolConfig"]["toolChoice"] == {"tool": {"name": "emit_stance"}}

    def test_default_model_from_env(self, monkeypatch):
        monkeypatch.delenv("MACRO_LLM_MODEL", raising=False)
        assert bj.BedrockJson(client=object()).model_id == bj.DEFAULT_MODEL_ID
        monkeypatch.setenv("MACRO_LLM_MODEL", "env-model")
        assert bj.BedrockJson(client=object()).model_id == "env-model"

    def test_token_callback_sums_input_and_output(self):
        client = FakeClient(
            [converse_ok({"a": "1"}, tokens=(10, 5)), converse_ok({"a": "2"}, tokens=(7, 3))]
        )
        seen: list[int] = []
        llm = bj.BedrockJson(client=client, on_tokens=seen.append)
        llm.invoke_json(system="s", user="u", schema=SIMPLE_SCHEMA)
        llm.invoke_json(system="s", user="u", schema=SIMPLE_SCHEMA)
        assert seen == [15, 10]
        assert llm.tokens_used == 25
        assert llm.calls == 2

    def test_budget_exceeded_blocks_call(self):
        client = FakeClient([converse_ok({"a": "1"})])
        asked: list[int] = []

        def budget(est):
            asked.append(est)
            return False

        llm = bj.BedrockJson(client=client, budget_check=budget)
        with pytest.raises(bj.BudgetExceeded):
            llm.invoke_json(system="s", user="x" * 300, schema=SIMPLE_SCHEMA, max_tokens=500)
        assert client.calls == []  # 호출 자체가 막혀야 한다
        assert asked == [300 // 3 + 500]

    def test_budget_ok_allows_call(self):
        client = FakeClient([converse_ok({"a": "1"})])
        llm = bj.BedrockJson(client=client, budget_check=lambda est: True)
        assert llm.invoke_json(system="s", user="u", schema=SIMPLE_SCHEMA) == {"a": "1"}

    def test_throttling_retries_with_exponential_backoff(self):
        client = FakeClient([FakeThrottling(), FakeThrottling(), converse_ok({"a": "ok"})])
        llm = bj.BedrockJson(client=client)
        waits: list[float] = []
        llm._sleep = waits.append
        assert llm.invoke_json(system="s", user="u", schema=SIMPLE_SCHEMA) == {"a": "ok"}
        assert len(client.calls) == 3
        assert waits == [1.0, 2.0]

    def test_throttling_gives_up_after_three_retries(self):
        client = FakeClient([FakeThrottling() for _ in range(5)])
        llm = bj.BedrockJson(client=client)
        llm._sleep = lambda _s: None
        with pytest.raises(FakeThrottling):
            llm.invoke_json(system="s", user="u", schema=SIMPLE_SCHEMA)
        assert len(client.calls) == bj.MAX_THROTTLE_RETRIES + 1

    def test_non_throttling_error_is_not_retried(self):
        client = FakeClient([ValueError("boom")])
        llm = bj.BedrockJson(client=client)
        with pytest.raises(ValueError):
            llm.invoke_json(system="s", user="u", schema=SIMPLE_SCHEMA)
        assert len(client.calls) == 1

    def test_missing_tool_use_raises(self):
        client = FakeClient([{"output": {"message": {"content": [{"text": "sorry"}]}}}])
        llm = bj.BedrockJson(client=client)
        with pytest.raises(ValueError, match="toolUse"):
            llm.invoke_json(system="s", user="u", schema=SIMPLE_SCHEMA)

    def test_schema_required_key_missing_raises(self):
        client = FakeClient([converse_ok({"n": 3})])
        llm = bj.BedrockJson(client=client)
        with pytest.raises(ValueError, match="필수 키 누락"):
            llm.invoke_json(system="s", user="u", schema=SIMPLE_SCHEMA)

    def test_schema_type_mismatch_raises(self):
        client = FakeClient([converse_ok({"a": 1})])
        llm = bj.BedrockJson(client=client)
        with pytest.raises(ValueError, match="타입 불일치"):
            llm.invoke_json(system="s", user="u", schema=SIMPLE_SCHEMA)

    def test_validate_nested_array_of_objects(self):
        bj.validate_against_schema({"polls": [{"pollster": "A", "results": {"x": 1.0}}]},
                                   {"type": "object",
                                    "properties": {"polls": {"type": "array", "items": {
                                        "type": "object",
                                        "properties": {"pollster": {"type": "string"},
                                                       "results": {"type": "object"}},
                                        "required": ["pollster", "results"]}}},
                                    "required": ["polls"]})
        with pytest.raises(ValueError):
            bj.validate_against_schema({"polls": [{"results": {}}]},
                                       {"type": "object",
                                        "properties": {"polls": {"type": "array", "items": {
                                            "type": "object", "required": ["pollster"]}}},
                                        "required": ["polls"]})

    def test_boolean_is_not_a_number(self):
        with pytest.raises(ValueError):
            bj.validate_against_schema({"n": True}, {"type": "object",
                                                    "properties": {"n": {"type": "number"}},
                                                    "required": ["n"]})

    def test_exported_schemas_are_well_formed(self):
        for schema in (
            bj.json_schema_stance, bj.json_schema_polls,
            bj.json_schema_energy, bj.json_schema_brief,
        ):
            assert schema["type"] == "object"
            assert schema["required"]
            for key in schema["required"]:
                assert key in schema["properties"], key


# ================================================================ cb_statements
CB_INDEX = """<html><body><ul>
<li><a href="/newsevents/pressreleases/monetary20240918a.htm">
    Federal Reserve issues FOMC statement</a></li>
<li><a href="/newsevents/pressreleases/monetary20240731a.htm">
    Federal Reserve issues FOMC statement</a></li>
<li><a href="/monetarypolicy/fomcminutes20240918.htm">Minutes of the FOMC meeting</a></li>
<li><a href="/newsevents/speech/powell20240920a.htm">Speech by Chair Powell</a></li>
</ul></body></html>"""
CB_INDEX_URL = "https://www.federalreserve.gov/newsevents/pressreleases/2024-press.htm"
CB_STMT_URL = "https://www.federalreserve.gov/newsevents/pressreleases/monetary20240918a.htm"

CB_META = {
    "US": {
        "name_ko": "연방준비제도",
        "name_en": "Federal Reserve",
        "statements_url": CB_INDEX_URL,
        "lang": "en",
    },
    "DE": {"refer": "EU", "name_ko": "유럽중앙은행 참조"},
}

GOOD_STANCE = {
    "stance_score": -1.2,
    "direction": "cut",
    "forward_guidance": "easing",
    "rate_after": 4.875,
    "statement_date": "2024-09-18",
    "meeting_type": "regular",
    "title_ko": "연준, 기준금리 50bp 인하",
    "summary_ko": "연준은 정책금리 목표범위를 0.5%p 낮췄다.",
    "quotes": [
        "the Committee decided to lower the target range for the federal funds rate by 1/2 percentage point",
        "Inflation has made further progress toward the Committee's 2 percent objective",
    ],
    "confidence": 0.82,
}


@pytest.fixture
def cb_ctx(monkeypatch):
    """US 성명 픽스처를 서비스하는 ctx + sources.yaml 대체 메타."""
    monkeypatch.setattr(cb, "load_sources", lambda *a, **k: {"central_banks": CB_META})

    def make(*, llm=None, no_llm=False, extra=None):
        return StubCtx(
            pages={CB_INDEX_URL: CB_INDEX, CB_STMT_URL: fixture("cb_statement_us.html")},
            llm=llm,
            no_llm=no_llm,
            extra=extra,
        )

    return make


class TestCbStatements:
    def test_module_contract(self):
        assert cb.SOURCE_NAME == "cb_statements"
        assert cb.CADENCE == "daily"

    def test_pick_statement_links_prefers_statement_over_minutes_and_speech(self):
        links = cb.pick_statement_links(CB_INDEX, CB_INDEX_URL, "US", limit=2)
        assert links[0][0] == CB_STMT_URL
        assert all("minutes" not in u.lower() and "speech" not in u for u, _ in links)

    def test_pick_statement_links_fail_open_on_unknown_country(self):
        html = '<a href="/pr/2026-09-17-decision.html">2026-09-17 금리 결정</a>'
        links = cb.pick_statement_links(html, "https://cb.example/pr/", "ZZ")
        assert links and links[0][0].endswith("2026-09-17-decision.html")

    def test_pick_statement_links_prefers_newest_date(self):
        html = """
        <a href="/pressreleases/monetary20260128a.htm">FOMC statement</a>
        <a href="/pressreleases/monetary20260916a.htm">FOMC statement</a>
        <a href="/pressreleases/monetary20260729a.htm">FOMC statement</a>"""
        links = cb.pick_statement_links(
            html, "https://f.example/pressreleases/2026-press.htm", "US",
            extra_keywords=["pressreleases/monetary"],
        )
        assert links[0][0].endswith("monetary20260916a.htm")

    def test_pick_statement_links_skips_index_self_and_binaries(self):
        base = "https://cb.example/mp/decisions"
        html = """
        <a href="/mp/decisions">Monetary policy decisions</a>
        <a href="/mp/decisions?year=2026">Monetary policy decisions 2026</a>
        <a href="/mp/statement-2026-09-11.pdf">Statement 11 September 2026</a>
        <a href="/mp/statement-2026-09-11.htm">Statement 11 September 2026</a>"""
        links = cb.pick_statement_links(html, base, "ZZ", limit=5)
        assert [u for u, _a in links] == ["https://cb.example/mp/statement-2026-09-11.htm"]

    def test_strict_mode_rejects_unrelated_pages(self):
        """link_keywords가 있으면 완화 단계를 끈다 (엉뚱한 페이지 판정 방지)."""
        html = '<a href="/markets/sonia-benchmark">SONIA benchmark</a>'
        base = "https://boe.example/mp/summary"
        assert cb.pick_statement_links(html, base, "GB", extra_keywords=["summary/20"]) == []
        # 키워드를 지정하지 않은 국가는 기존처럼 일반 키워드로 완화(fail-open)
        open_html = html + '<a href="/mp/decision-x.htm">Monetary policy decision</a>'
        assert cb.pick_statement_links(open_html, base, "ZZ")[0][0].endswith("decision-x.htm")

    def test_short_url_date_patterns(self):
        assert cb._date_from_url("https://x/en/mopo/k260731a.htm") == date(2026, 7, 31)
        assert cb._date_from_url("https://x/press/pr/ecb.mp260910~31.en.html") == date(2026, 9, 10)
        assert cb._date_from_url("https://x/2026/09/fad-press-release-2026-09-02/") == date(
            2026, 9, 2
        )
        assert cb._date_from_url("https://x/2026/07/other/") is None
        assert cb._date_from_url("https://x/2026/07/other/", allow_month_only=True) == date(
            2026, 7, 1
        )

    def test_links_from_rss_feed(self):
        xml = """<?xml version="1.0"?><rss version="2.0"><channel>
        <item><title>ECB monetary policy decisions</title>
          <link>https://ecb.example/press/pr/date/2026/html/ecb.mp260910~a.en.html</link>
          <pubDate>Thu, 10 Sep 2026 14:15:00 +0200</pubDate></item>
        <item><title>Older decision</title>
          <link>https://ecb.example/press/pr/date/2026/html/ecb.mp260611~b.en.html</link>
          <pubDate>Thu, 11 Jun 2026 14:15:00 +0200</pubDate></item>
        </channel></rss>"""
        items = cb.links_from_feed(xml)
        assert len(items) == 2
        assert items[0][0].endswith("ecb.mp260910~a.en.html")
        assert "2026-09-10" in items[0][1]
        picked = cb.pick_statement_links(
            "", "https://ecb.example/rss", "EU", extra_keywords=["ecb.mp"], candidates=items
        )
        assert picked[0][0].endswith("ecb.mp260910~a.en.html")

    def test_links_from_feed_drops_executable_schemes(self):
        xml = """<rss><channel>
        <item><title>Bad js</title><link>javascript:alert(1)</link>
          <pubDate>Thu, 10 Sep 2026 14:15:00 +0200</pubDate></item>
        <item><title>Bad data</title><link>data:text/html,hi</link></item>
        <item><title>Bad vb</title><link> VBSCRIPT:msgbox</link></item>
        <item><title>Good</title><link>/press/2026/ok.html</link>
          <pubDate>Thu, 11 Jun 2026 14:15:00 +0200</pubDate></item>
        </channel></rss>"""
        items = cb.links_from_feed(xml, "https://ecb.example/rss")
        assert [href for href, _ in items] == ["https://ecb.example/press/2026/ok.html"]
        assert "2026-06-11" in items[0][1]
        # base_url이 없어도 스킴 필터는 동작한다 (상대 링크는 그대로 남는다)
        assert [href for href, _ in cb.links_from_feed(xml)] == ["/press/2026/ok.html"]

    def test_links_from_atom_feed(self):
        xml = """<feed xmlns="http://www.w3.org/2005/Atom">
        <entry><title>Copom reduz a taxa Selic</title>
          <link rel="alternate" href="/controleinflacao/comunicadoscopom/21261"/>
          <updated>2026-09-16T18:30:00Z</updated></entry></feed>"""
        (item,) = cb.links_from_feed(xml, "https://bcb.example/api/feed")
        assert item[0] == "https://bcb.example/controleinflacao/comunicadoscopom/21261"
        assert "2026-09-16" in item[1]

    def test_feed_index_is_detected_and_used(self, monkeypatch):
        """statements_url 자체가 Atom 피드인 경우(브라질)도 링크를 찾아야 한다."""
        feed_url = "https://bcb.example/api/feed/copom"
        xml = """<feed xmlns="http://www.w3.org/2005/Atom"><entry>
          <title>Comunicado do Copom</title>
          <link rel="alternate" href="https://bcb.example/copom/21261"/>
          <updated>2026-09-16T18:30:00Z</updated></entry></feed>"""
        links = cb._links_from_html_or_feed(xml, feed_url, "BR", 1, ["copom"])
        assert links == [("https://bcb.example/copom/21261", "Comunicado do Copom 2026-09-16")]

    def test_builds_doc_and_observation(self, cb_ctx):
        llm = StubLLM([GOOD_STANCE])
        ctx = cb_ctx(llm=llm)
        docs = cb.collect_docs([Country("US", "미국")], ctx)
        assert len(docs) == 1
        doc = docs[0]
        assert (doc.type, doc.iso, doc.date) == ("cb_stance", "US", "2024-09-18")
        assert doc.id == tu.sha12(CB_STMT_URL) and len(doc.id) == 12
        assert doc.pk == "DOC#cb_stance#US" and doc.sk == f"2024-09-18#{doc.id}"
        assert doc.source_url == CB_STMT_URL
        assert doc.source_name == "연방준비제도"
        assert doc.ai_generated is True
        assert doc.model_id == "stub-model-1"
        assert doc.review_status == "pending"  # AI 산출물은 검토 전
        assert len(doc.quotes) == 2
        assert doc.payload["stance_score"] == -1.0  # -1.2 → 0.5 단위 반올림
        assert doc.payload["direction"] == "cut"
        assert doc.payload["forward_guidance"] == "easing"
        assert doc.payload["rate_after"] == pytest.approx(4.875)
        assert doc.payload["statement_date"] == "2024-09-18"
        assert doc.s3_key == f"macro/docs/cb_stance/US/2024-09-18_{doc.id}.md"
        assert ctx.errors == []
        # 같은 ctx에서 collect()는 네트워크·LLM을 다시 쓰지 않는다 (캐시)
        obs = cb.collect([Country("US", "미국")], [], ctx)
        assert len(llm.calls) == 1
        (ob,) = obs
        assert (ob.indicator, ob.iso, ob.freq, ob.period) == ("cb_stance", "US", "E", "2024-09-18")
        assert ob.value == -1.0 and ob.unit == "score"
        assert ob.source == "cb_statements" and ob.flags == ["ai_generated"]
        assert ob.payload["doc_id"] == doc.id
        assert ob.pk == "OBS#cb_stance#US" and ob.sk == "E#2024-09-18"

    def test_doc_text_is_left_for_the_collector(self, cb_ctx):
        # collect.py의 _put_docs가 이 키로 원문을 읽어 store.save_doc_text에 넘긴다
        ctx = cb_ctx(llm=StubLLM([GOOD_STANCE]))
        (doc,) = cb.collect_docs([Country("US", "미국")], ctx)
        text = ctx.extra["doc_texts"][f"cb_stance/US/{doc.id}"]
        assert "lower the target range for the federal funds rate" in text
        assert "절단" not in text  # 12,000자 미만이라 절단 표기 없음

    def test_doc_text_marks_truncation_at_body_limit(self, monkeypatch):
        monkeypatch.setattr(cb, "load_sources", lambda *a, **k: {"central_banks": CB_META})
        body = (
            "<html><body><main><p>"
            + "The Committee decided to lower the target range for the federal funds rate. " * 300
            + "</p></main></body></html>"
        )
        ctx = StubCtx(
            pages={CB_INDEX_URL: CB_INDEX, CB_STMT_URL: body},
            llm=StubLLM([dict(GOOD_STANCE, quotes=["decided to lower the target range for the federal funds rate"])]),
        )
        (doc,) = cb.collect_docs([Country("US", "미국")], ctx)
        text = ctx.extra["doc_texts"][f"cb_stance/US/{doc.id}"]
        assert text.endswith("… (12,000자에서 절단)")
        assert len(text) <= common.DOC_TEXT_MAX_CHARS + 40

    def test_prompt_includes_score_rules_and_full_body(self, cb_ctx):
        llm = StubLLM([GOOD_STANCE])
        cb.collect_docs([Country("US", "미국")], cb_ctx(llm=llm))
        call = llm.calls[0]
        assert "0.5 단위" in call["system"] and "원문" in call["system"]
        assert call["schema"] is bj.json_schema_stance
        assert "Recent indicators suggest" in call["user"]
        assert len(call["user"]) < cb.MAX_BODY_CHARS + 1000

    @pytest.mark.parametrize(
        ("score", "expected"),
        [(-1.2, -1.0), (0.7, 0.5), (0.26, 0.5), (0.24, 0.0), (9.0, 2.0), (-9.0, -2.0)],
    )
    def test_stance_score_rounded_to_half_and_clamped(self, cb_ctx, score, expected):
        payload = dict(GOOD_STANCE, stance_score=score)
        docs = cb.collect_docs([Country("US", "미국")], cb_ctx(llm=StubLLM([payload])))
        assert docs[0].payload["stance_score"] == expected

    def test_quote_not_in_text_is_dropped_and_doc_kept_if_one_survives(self, cb_ctx):
        payload = dict(GOOD_STANCE, quotes=["완전히 없는 문장입니다", GOOD_STANCE["quotes"][0]])
        ctx = cb_ctx(llm=StubLLM([payload]))
        docs = cb.collect_docs([Country("US", "미국")], ctx)
        assert len(docs) == 1
        assert docs[0].quotes == [GOOD_STANCE["quotes"][0]]

    def test_all_quotes_absent_discards_doc_and_records_error(self, cb_ctx):
        payload = dict(GOOD_STANCE, quotes=["없는 인용 1", "없는 인용 2"])
        ctx = cb_ctx(llm=StubLLM([payload]))
        assert cb.collect_docs([Country("US", "미국")], ctx) == []
        assert any("인용문이 원문에 없어" in e for e in ctx.errors)

    def test_invalid_enum_is_inferred_from_score(self, cb_ctx):
        payload = dict(GOOD_STANCE, direction="dovish", forward_guidance="???")
        docs = cb.collect_docs([Country("US", "미국")], cb_ctx(llm=StubLLM([payload])))
        assert docs[0].payload["direction"] == "cut"
        assert docs[0].payload["forward_guidance"] == "easing"

    def test_euro_member_referring_to_eu_is_skipped(self, cb_ctx):
        ctx = cb_ctx(llm=StubLLM([GOOD_STANCE]))
        assert cb.collect_docs([Country("DE", "독일", euro="yes")], ctx) == []
        assert ctx.requested == []  # 네트워크 호출조차 하지 않는다

    def test_no_llm_saves_raw_only(self, cb_ctx):
        ctx = cb_ctx(no_llm=True, llm=StubLLM([GOOD_STANCE]))
        assert cb.collect_docs([Country("US", "미국")], ctx) == []
        assert [n for _s, n, _ln in ctx.raw] == ["US_index", f"US_{tu.sha12(CB_STMT_URL)}"]
        assert ctx.errors == []

    def test_missing_llm_client_saves_raw_only(self, cb_ctx):
        ctx = cb_ctx(llm=None)
        assert cb.collect_docs([Country("US", "미국")], ctx) == []
        assert len(ctx.raw) == 2

    def test_seen_urls_skips_statement(self, cb_ctx):
        llm = StubLLM([GOOD_STANCE])
        ctx = cb_ctx(llm=llm, extra={"seen_urls": {CB_STMT_URL}})
        assert cb.collect_docs([Country("US", "미국")], ctx) == []
        assert llm.calls == []
        assert CB_STMT_URL not in ctx.requested  # 본문도 받지 않는다

    def test_llm_failure_is_isolated_per_country(self, cb_ctx):
        ctx = cb_ctx(llm=StubLLM([bj.BudgetExceeded("예산 초과")]))
        assert cb.collect_docs([Country("US", "미국")], ctx) == []
        assert any("LLM 판정 실패" in e for e in ctx.errors)

    def test_index_fetch_failure_records_error(self, monkeypatch):
        monkeypatch.setattr(cb, "load_sources", lambda *a, **k: {"central_banks": CB_META})
        ctx = StubCtx(pages={}, llm=StubLLM([GOOD_STANCE]))
        assert cb.collect_docs([Country("US", "미국")], ctx) == []
        assert any("HTTP 404" in e for e in ctx.errors)

    def test_statement_date_falls_back_to_today_when_unparseable(self):
        got = cb._statement_date(None, "https://x.example/press/latest", "본문에 날짜 없음")
        assert got == date.today().isoformat() or len(got) == 10

    def test_statement_date_rejects_future(self):
        future = (TODAY + timedelta(days=3650)).isoformat()
        assert cb._statement_date(future, "https://x.example/p", "") != future


# ================================================================ polls_wiki
KR_ALIASES = {
    "더불어민주당": ["DPK"], "국민의힘": ["PPP"], "조국혁신당": ["RKP"],
    "개혁신당": ["RP"], "진보당": ["PP"], "기본소득당": ["BIP"], "사회민주당": ["SDP"],
}
DE_ALIASES = {
    "기독민주·기사연합": ["Union", "CDU/CSU"], "독일을위한대안": ["AfD"],
    "사회민주당": ["SPD"], "녹색당": ["Grüne"], "좌파당": ["Linke"],
    "자유민주당": ["FDP"], "자유와정의연합": ["BSW"],
}
GB_ALIASES = {
    "노동당": ["Lab"], "보수당": ["Con"], "리폼UK": ["Ref"], "자유민주당": ["LD"],
    "녹색당": ["Grn"], "스코틀랜드국민당": ["SNP"], "플라이드컴리": ["PC"],
}
POLL_META = {
    "KR": {
        "wiki_page": "Opinion_polling_for_the_next_South_Korean_legislative_election",
        "party_aliases": KR_ALIASES,
        "trust": "높음",
        "election": {
            "next_election_date": "2028-04",
            "election_type": "legislative",
            "ruling_party_ko": "더불어민주당",
            "ruling_lean": "center-left",
            "second_party_ko": "국민의힘",
            "system_note": "지역구 254석 + 비례 46석 준연동형.",
        },
    },
    "CN": {"not_applicable_reason": "경쟁 정당이 없어 정당 지지율 조사가 존재하지 않는다."},
}
KR_URL = pw.WIKI_BASE + POLL_META["KR"]["wiki_page"]


def parse_fixture(name: str, aliases: dict) -> list[dict]:
    amap = pw.build_alias_map(aliases)
    tables = pw.select_poll_tables(tu.extract_tables(fixture(name), css_class="wikitable"), amap)
    out: list[dict] = []
    for t in tables:
        out.extend(pw.parse_poll_table(t, amap, today=TODAY))
    return out


class TestPollsWikiRuleParser:
    def test_module_contract(self):
        assert pw.SOURCE_NAME == "wiki_polls"
        assert pw.CADENCE == "weekly"

    @pytest.mark.parametrize(
        ("name", "aliases", "expect_party"),
        [
            ("wiki_polls_kr.html", KR_ALIASES, "더불어민주당"),
            ("wiki_polls_de.html", DE_ALIASES, "독일을위한대안"),
            ("wiki_polls_gb.html", GB_ALIASES, "노동당"),
        ],
    )
    def test_extracts_polls_from_real_tables(self, name, aliases, expect_party):
        polls = parse_fixture(name, aliases)
        assert len(polls) >= 1
        for p in polls:
            assert p["pollster"] and len(p["results"]) >= 2
            assert tu.parse_date_range(p["fieldwork_end"])[1] is not None
            assert sum(p["results"].values()) <= pw.MAX_RESULT_SUM
            assert all(0 <= v <= 100 for v in p["results"].values())
        assert expect_party in polls[0]["results"]

    def test_korean_table_values_and_year_from_section_heading(self):
        polls = parse_fixture("wiki_polls_kr.html", KR_ALIASES)
        top = polls[0]
        assert top["pollster"] == "Gallup Korea"
        # 표의 날짜 칸("8–10 Sep")에 연도가 없어 절 제목 2026으로 보정된다
        assert top["fieldwork_start"] == "2026-09-08"
        assert top["fieldwork_end"] == "2026-09-10"
        assert top["sample_size"] == 1000
        assert top["results"]["더불어민주당"] == 38.0
        assert top["results"]["국민의힘"] == 29.0
        assert top["ai"] is False

    def test_non_party_columns_are_excluded(self):
        polls = parse_fixture("wiki_polls_kr.html", KR_ALIASES)
        keys = set(polls[0]["results"])
        assert not keys & {"Others", "Und./ no ans.", "Lead", "Margin of error", "Sample size"}

    def test_unmapped_party_header_keeps_original_name(self):
        polls = parse_fixture("wiki_polls_gb.html", GB_ALIASES)
        # 픽스처의 'RB'(Restore Britain)는 별칭에 없으므로 원문 헤더가 유지된다
        assert any("RB" in p["results"] for p in polls)

    def test_german_percent_with_decimal_and_dash_cells(self):
        polls = parse_fixture("wiki_polls_de.html", DE_ALIASES)
        assert polls[0]["pollster"] == "pollytix"
        assert polls[0]["results"]["독일을위한대안"] == 27.0
        assert polls[0]["sample_size"] == 3113

    def test_rejects_sum_over_105(self):
        assert not pw.validate_poll(
            {"fieldwork_end": "2026-09-01", "results": {"A": 60.0, "B": 50.0}}, today=TODAY
        )
        assert pw.validate_poll(
            {"fieldwork_end": "2026-09-01", "results": {"A": 60.0, "B": 44.0}}, today=TODAY
        )

    def test_rejects_future_and_stale_dates(self):
        future = (TODAY + timedelta(days=1)).isoformat()
        stale = (TODAY - timedelta(days=pw.MAX_AGE_DAYS + 1)).isoformat()
        base = {"results": {"A": 40.0, "B": 30.0}}
        assert not pw.validate_poll({**base, "fieldwork_end": future}, today=TODAY)
        assert not pw.validate_poll({**base, "fieldwork_end": stale}, today=TODAY)
        assert not pw.validate_poll({**base, "fieldwork_end": "2026-13-40"}, today=TODAY)
        assert not pw.validate_poll({**base, "fieldwork_end": None}, today=TODAY)

    def test_rejects_out_of_range_values_and_single_party(self):
        assert not pw.validate_poll(
            {"fieldwork_end": "2026-09-01", "results": {"A": 101.0, "B": 1.0}}, today=TODAY
        )
        assert not pw.validate_poll(
            {"fieldwork_end": "2026-09-01", "results": {"A": 40.0}}, today=TODAY
        )

    def test_seat_projection_row_rejected_by_sum_rule(self):
        html = """
        <h3>2026</h3>
        <table class="wikitable"><tr><th>Date(s) conducted</th><th>Pollster</th>
        <th>Lab</th><th>Con</th><th>Ref</th></tr>
        <tr><td>1-3 Sep</td><td>Survation</td><td>245</td><td>99</td><td>174</td></tr>
        </table>"""
        (t,) = tu.extract_tables(html, css_class="wikitable")
        assert pw.parse_poll_table(t, pw.build_alias_map(GB_ALIASES), today=TODAY) == []

    def test_election_result_row_is_skipped(self):
        html = """
        <h3>2026</h3>
        <table class="wikitable"><tr><th>Polling firm</th><th>Fieldwork date</th>
        <th>Union</th><th>AfD</th></tr>
        <tr><td>2025 federal election</td><td>23 Feb 2026</td><td>28.5</td><td>20.8</td></tr>
        <tr><td>INSA</td><td>1-3 Sep 2026</td><td>19</td><td>27</td></tr>
        </table>"""
        (t,) = tu.extract_tables(html, css_class="wikitable")
        polls = pw.parse_poll_table(t, pw.build_alias_map(DE_ALIASES), today=TODAY)
        assert [p["pollster"] for p in polls] == ["INSA"]

    def test_looks_like_poll_table_needs_two_parties_and_a_date(self):
        amap = pw.build_alias_map(GB_ALIASES)
        (bad,) = tu.extract_tables(
            '<table class="wikitable"><tr><th>Year</th><th>Winner</th></tr>'
            "<tr><td>2024</td><td>Lab</td></tr></table>"
        )
        assert not pw.looks_like_poll_table(bad, amap)
        good = tu.extract_tables(fixture("wiki_polls_gb.html"), css_class="wikitable")[0]
        assert pw.looks_like_poll_table(good, amap)

    def test_gov_approval_column_is_picked_up(self):
        html = """
        <h3>2026</h3>
        <table class="wikitable"><tr><th>Polling firm</th><th>Fieldwork date</th>
        <th>DPK</th><th>PPP</th><th>Government approval</th></tr>
        <tr><td>Gallup</td><td>1-3 Sep 2026</td><td>38</td><td>29</td><td>52</td></tr>
        </table>"""
        (t,) = tu.extract_tables(html, css_class="wikitable")
        (poll,) = pw.parse_poll_table(t, pw.build_alias_map(KR_ALIASES), today=TODAY)
        assert poll["gov_approval"] == 52.0
        assert set(poll["results"]) == {"더불어민주당", "국민의힘"}

    def test_token_alias_matching_for_group_prefixed_headers(self):
        """실측: 호주 헤더는 'Primary vote ALP', 브라질은 'Lula PT' 형태다."""
        amap = pw.build_alias_map({"호주노동당": ["ALP"], "호주녹색당": ["GRN"]})
        assert pw.match_alias("Primary vote ALP", amap) == "호주노동당"
        assert pw.match_alias("2PP vote ALP", amap) == "호주노동당"
        assert pw.match_alias("GRN", amap) == "호주녹색당"
        assert pw.match_alias("Sample size", amap) is None

    def test_colspan_duplicated_cell_is_not_double_counted(self):
        """호주 실측: L/NP 한 칸이 colspan=2로 두 칸에 복제되어 합이 120%가 된다."""
        html = """
        <h3>2026</h3>
        <table class="wikitable">
          <tr><th rowspan="2">Fieldwork date</th><th rowspan="2">Polling firm</th>
              <th rowspan="2">ALP</th><th colspan="2">L/NP</th><th rowspan="2">GRN</th></tr>
          <tr><th>LIB</th><th>NAT</th></tr>
          <tr><td>1-3 Sep 2026</td><td>DemosAU</td><td>28</td>
              <td colspan="2">40</td><td>13</td></tr>
        </table>"""
        (t,) = tu.extract_tables(html, css_class="wikitable")
        amap = pw.build_alias_map(
            {"호주노동당": ["ALP"], "자유당": ["LIB"], "국민당": ["NAT"], "호주녹색당": ["GRN"]}
        )
        (poll,) = pw.parse_poll_table(t, amap, today=TODAY)
        # 40이 LIB/NAT 두 칸에 복제됐지만 한 번만 반영된다 → 합계 81 (161이 아님)
        assert sum(poll["results"].values()) == 81.0
        assert poll["results"]["호주노동당"] == 28.0

    def test_exclude_columns_drops_coalition_totals(self):
        """이탈리아 실측: 'Coalitions CSX/CDX' 합계 컬럼 때문에 합이 105%를 넘는다."""
        html = """
        <h3>2026</h3>
        <table class="wikitable">
          <tr><th rowspan="2">Fieldwork date</th><th rowspan="2">Polling firm</th>
              <th colspan="2">Parties</th><th colspan="2">Coalitions</th></tr>
          <tr><th>FdI</th><th>PD</th><th>CSX</th><th>CDX</th></tr>
          <tr><td>16-17 Sep 2026</td><td>Termometro</td><td>27.1</td><td>21.4</td>
              <td>44.1</td><td>41.1</td></tr>
          <tr><td>14-15 Sep 2026</td><td>Only Numbers</td><td>27.3</td><td>20.1</td>
              <td>43.2</td><td>42.9</td></tr>
          <tr><td>10-11 Sep 2026</td><td>SWG</td><td>28.0</td><td>21.0</td>
              <td>44.0</td><td>42.0</td></tr>
        </table>"""
        (t,) = tu.extract_tables(html, css_class="wikitable")
        amap = pw.build_alias_map({"이탈리아의형제들": ["FdI"], "민주당": ["PD"]})
        assert pw.parse_poll_table(t, amap, today=TODAY) == []  # 합계 133.7 → 전부 거부
        exclude = {pw._alias_key(x) for x in ("CSX", "CDX")}
        polls = pw.parse_poll_table(t, amap, today=TODAY, exclude=exclude)
        assert len(polls) == 3
        assert polls[0]["results"] == {"이탈리아의형제들": 27.1, "민주당": 21.4}

    def test_merged_group_header_lead_and_others_excluded(self):
        """2행 헤더가 합쳐진 'Parties Lead'·'Primary vote Others'도 제외돼야 한다."""
        amap = pw.build_alias_map({"민주당": ["PD"]})
        html = """<table class="wikitable"><tr><th>Fieldwork date</th><th>Polling firm</th>
        <th>Parties PD</th><th>Parties FdI</th><th>Parties Others</th><th>Parties Lead</th></tr>
        <tr><td>1-3 Sep 2026</td><td>X</td><td>21</td><td>27</td><td>5</td><td>6</td></tr>
        </table>"""
        (t,) = tu.extract_tables(html, css_class="wikitable")
        names = set(pw.classify_columns(t, amap)["parties"].values())
        assert "민주당" in names
        assert not {"Parties Others", "Parties Lead"} & names

    @pytest.mark.parametrize(
        "heading",
        ["Seat projections", "18–34", "65+", "Generation Z", "Preferred prime minister",
         "Coalition scenarios", "Hypothetical polling", "Regional breakdown"],
    )
    def test_heading_blocked_for_non_national_tables(self, heading):
        assert pw.heading_blocked(heading)

    @pytest.mark.parametrize("heading", ["2026", "2025", "Table of polls", "Aug–Oct (Campaign)", ""])
    def test_heading_allowed_for_main_tables(self, heading):
        assert not pw.heading_blocked(heading)

    def test_heading_exclude_from_sources_yaml(self):
        assert pw.heading_blocked("Scotland", {"Scotland", "Wales"})
        assert not pw.heading_blocked("Scotland", {"Wales"})

    def test_select_prefers_year_heading_and_skips_tiny_side_table(self):
        """1순위는 연도 제목 표, 2순위부터는 5행 이상만 (영국 'Clacton' 1행 표 방지)."""
        rows = "".join(
            f"<tr><td>{d}-{d+2} Sep 2026</td><td>P{d}</td><td>27</td><td>18</td></tr>"
            for d in range(1, 9)
        )
        html = f"""
        <h3>Clacton</h3>
        <table class="wikitable"><tr><th>Date</th><th>Pollster</th><th>Lab</th><th>Con</th></tr>
        <tr><td>16-18 Sep 2026</td><td>Survation</td><td>10</td><td>12</td></tr></table>
        <h3>2026</h3>
        <table class="wikitable"><tr><th>Date</th><th>Pollster</th><th>Lab</th><th>Con</th></tr>
        {rows}</table>"""
        amap = pw.build_alias_map({"노동당": ["Lab"], "보수당": ["Con"]})
        picked = pw.select_poll_tables(
            tu.extract_tables(html, css_class="wikitable"), amap, max_tables=2, today=TODAY
        )
        assert [t.heading for t in picked] == ["2026"]

    def test_year_comes_from_section_heading_not_today(self):
        """날짜 칸에 연도가 없으면 절 제목 연도로 보정한다 (한국·영국 표 실측)."""
        html = """<h3>2025</h3><table class="wikitable">
        <tr><th>Fieldwork date</th><th>Polling firm</th><th>DPK</th><th>PPP</th></tr>
        <tr><td>8–10 Sep</td><td>Gallup</td><td>38</td><td>29</td></tr></table>"""
        (t,) = tu.extract_tables(html, css_class="wikitable")
        (poll,) = pw.parse_poll_table(t, pw.build_alias_map(KR_ALIASES), today=TODAY)
        assert poll["fieldwork_end"] == "2025-09-10"

    def test_duplicate_alias_maps_only_first_column(self):
        amap = pw.build_alias_map({"개혁신당": ["RP", "NFP"]})
        html = """<table class="wikitable"><tr><th>Fieldwork date</th><th>Polling firm</th>
        <th>RP</th><th>NFP</th><th>DPK</th></tr>
        <tr><td>1-3 Sep 2026</td><td>X</td><td>3</td><td>1</td><td>38</td></tr></table>"""
        (t,) = tu.extract_tables(html, css_class="wikitable")
        roles = pw.classify_columns(t, amap)
        assert list(roles["parties"].values()).count("개혁신당") == 1


class TestPollsWikiDocs:
    @pytest.fixture
    def poll_ctx(self, monkeypatch):
        monkeypatch.setattr(pw, "load_sources", lambda *a, **k: {"polls": POLL_META})

        def make(*, llm=None, no_llm=False, pages=None):
            return StubCtx(
                pages=pages if pages is not None else {KR_URL: fixture("wiki_polls_kr.html")},
                llm=llm,
                no_llm=no_llm,
            )

        return make

    def test_poll_and_election_docs_and_observations(self, poll_ctx):
        ctx = poll_ctx()
        docs = pw.collect_docs([Country("KR", "한국")], ctx)
        election = [d for d in docs if d.type == "election"]
        polls = [d for d in docs if d.type == "poll"]
        assert len(election) == 1 and len(polls) >= 1
        (el,) = election
        assert el.ai_generated is False and el.review_status == "approved"
        assert el.payload["next_election_date"] == "2028-04"
        assert el.payload["ruling_party"] == "더불어민주당"
        assert el.payload["second_party"] == "국민의힘"
        assert el.payload["ruling_lean"] == "center-left"
        assert el.payload["poll_trust"] == "높음"
        assert set(el.payload) >= {
            "next_election_date", "election_type", "ruling_party",
            "ruling_lean", "second_party", "system_note",
        }

        top = polls[0]
        assert top.iso == "KR" and top.date == top.payload["fieldwork_end"]
        assert top.id == tu.sha12(f"{top.payload['pollster']}{top.date}KR")
        assert top.ai_generated is False and top.quotes and "Gallup" in top.quotes[0]
        assert top.source_url == KR_URL and top.source_name == "위키피디아"
        assert set(top.payload) == {
            "pollster", "fieldwork_start", "fieldwork_end", "sample_size",
            "results", "gov_approval", "method",
        }

        obs = pw.collect([Country("KR", "한국")], [], ctx)
        support = [o for o in obs if o.indicator == "party_support"]
        assert len(support) == len(polls)
        o = support[0]
        assert (o.freq, o.unit, o.source) == ("W", "%", "wiki_polls")
        assert o.period == top.payload["fieldwork_end"]
        assert o.value == top.payload["results"]["더불어민주당"]
        assert o.payload["ruling_party"] == "더불어민주당"
        assert o.payload["results"] == top.payload["results"]
        assert o.flags == []

    def test_polls_are_deduplicated_by_pollster_and_date(self, poll_ctx):
        docs = pw.collect_docs([Country("KR", "한국")], poll_ctx())
        keys = [(d.payload["pollster"], d.date) for d in docs if d.type == "poll"]
        assert len(keys) == len(set(keys))

    def test_not_applicable_doc_for_china(self, poll_ctx):
        docs = pw.collect_docs([Country("CN", "중국")], poll_ctx(pages={}))
        (doc,) = docs
        assert doc.type == "not_applicable" and doc.iso == "CN"
        assert doc.payload["reason"].startswith("경쟁 정당이 없어")
        assert doc.ai_generated is False
        assert doc.pk == "DOC#not_applicable#CN"

    def test_page_fetch_failure_still_emits_election_doc(self, poll_ctx):
        ctx = poll_ctx(pages={})
        docs = pw.collect_docs([Country("KR", "한국")], ctx)
        assert [d.type for d in docs] == ["election"]
        assert any("HTTP 404" in e for e in ctx.errors)

    def test_country_without_meta_is_ignored(self, poll_ctx):
        assert pw.collect_docs([Country("ZZ", "없는나라")], poll_ctx(pages={})) == []

    def test_raw_html_is_saved(self, poll_ctx):
        ctx = poll_ctx()
        pw.collect_docs([Country("KR", "한국")], ctx)
        assert ("wiki_polls", "KR_polls", len(fixture("wiki_polls_kr.html"))) in ctx.raw


class TestPollsWikiLlmFallback:
    """규칙 파서가 0건을 낸 표만 LLM으로 보낸다."""

    HTML = """
    <h3>2026</h3>
    <table class="wikitable">
      <tr><th>Polling firm</th><th>Fieldwork date</th><th>DPK</th><th>PPP</th></tr>
      <tr><td>Gallup Korea</td><td>week 38 (unparseable)</td><td>38</td><td>29</td></tr>
    </table>"""

    def table(self):
        (t,) = tu.extract_tables(self.HTML, css_class="wikitable")
        return t

    def test_rule_parser_yields_nothing_for_broken_dates(self):
        assert pw.parse_poll_table(self.table(), pw.build_alias_map(KR_ALIASES), today=TODAY) == []

    def test_llm_normalizes_table_and_marks_ai_generated(self, monkeypatch):
        monkeypatch.setattr(pw, "load_sources", lambda *a, **k: {"polls": POLL_META})
        row = "Gallup Korea | week 38 (unparseable) | 38 | 29"
        llm = StubLLM([{"polls": [{
            "pollster": "Gallup Korea", "fieldwork_start": "2026-09-14",
            "fieldwork_end": "2026-09-16", "sample_size": 1000,
            "results": {"DPK": 38, "PPP": 29, "Others": 4}, "row_text": row,
        }]}])
        ctx = StubCtx(pages={KR_URL: self.HTML}, llm=llm)
        docs = pw.collect_docs([Country("KR", "한국")], ctx)
        polls = [d for d in docs if d.type == "poll"]
        assert len(polls) == 1
        doc = polls[0]
        assert doc.ai_generated is True and doc.review_status == "pending"
        assert doc.quotes == [row]
        assert doc.payload["results"] == {"더불어민주당": 38.0, "국민의힘": 29.0}
        assert doc.payload["sample_size"] == 1000
        assert llm.calls[0]["schema"] is bj.json_schema_polls
        assert "week 38" in llm.calls[0]["user"]
        obs = pw.collect([Country("KR", "한국")], [], ctx)
        assert [o.flags for o in obs if o.indicator == "party_support"] == [["ai_generated"]]

    def test_llm_row_not_in_table_is_discarded(self, monkeypatch):
        monkeypatch.setattr(pw, "load_sources", lambda *a, **k: {"polls": POLL_META})
        llm = StubLLM([{"polls": [{
            "pollster": "Ghost Poll", "fieldwork_end": "2026-09-16",
            "results": {"DPK": 38, "PPP": 29}, "row_text": "표에 없는 행 텍스트입니다",
        }]}])
        ctx = StubCtx(pages={KR_URL: self.HTML}, llm=llm)
        docs = pw.collect_docs([Country("KR", "한국")], ctx)
        assert [d.type for d in docs] == ["election"]

    def test_no_llm_skips_fallback(self, monkeypatch):
        monkeypatch.setattr(pw, "load_sources", lambda *a, **k: {"polls": POLL_META})
        llm = StubLLM([{"polls": []}])
        ctx = StubCtx(pages={KR_URL: self.HTML}, llm=llm, no_llm=True)
        docs = pw.collect_docs([Country("KR", "한국")], ctx)
        assert [d.type for d in docs] == ["election"]
        assert llm.calls == []


# ================================================================ energy_policy
ENERGY_TEXT = (
    "<html><body><main><p>"
    + "Korea aims to raise the share of renewables in power generation to 32.9% by 2038. "
    + "The plan also targets nuclear generation of about 35% by 2038. " * 6
    + "</p></main></body></html>"
)
ENERGY_URL = "https://www.iea.org/countries/korea"
ENERGY_META = {"KR": {"sources": [ENERGY_URL]}}
GOOD_ENERGY = {
    "targets": ["2038년까지 재생에너지 발전 비중 32.9% 달성", "2038년 원자력 발전 비중 약 35%"],
    "recent_changes": ["제11차 전력수급기본계획 확정"],
    "title_ko": "한국 에너지 정책 목표",
    "summary_ko": "재생에너지와 원자력 비중을 동시에 확대하는 계획이다.",
    "quotes": ["raise the share of renewables in power generation to 32.9% by 2038"],
    "confidence": 0.8,
}


class TestEnergyPolicy:
    @pytest.fixture
    def energy_ctx(self, monkeypatch):
        monkeypatch.setattr(ep, "load_sources", lambda *a, **k: {"energy_policy": ENERGY_META})

        def make(*, llm=None, no_llm=False, extra=None, pages=None):
            return StubCtx(
                pages=pages if pages is not None else {ENERGY_URL: ENERGY_TEXT},
                llm=llm, no_llm=no_llm, extra=extra,
            )

        return make

    def test_module_contract(self):
        assert ep.SOURCE_NAME == "energy_policy" and ep.CADENCE == "weekly"

    def test_builds_doc_with_contract_payload(self, energy_ctx):
        ctx = energy_ctx(llm=StubLLM([GOOD_ENERGY]))
        (doc,) = ep.collect_docs([Country("KR", "한국")], ctx)
        assert doc.type == "energy_policy" and doc.iso == "KR"
        assert set(doc.payload) == {"targets", "recent_changes", "sources", "content_sha1"}
        assert doc.payload["sources"] == [ENERGY_URL]
        assert doc.ai_generated is True and doc.review_status == "pending"
        assert doc.quotes == GOOD_ENERGY["quotes"]
        assert doc.model_id == "stub-model-1"
        assert ep.collect([Country("KR", "한국")], [], ctx) == []

    def test_doc_id_is_derived_from_content_hash(self, energy_ctx):
        (doc,) = ep.collect_docs([Country("KR", "한국")], energy_ctx(llm=StubLLM([GOOD_ENERGY])))
        assert doc.id == tu.sha12(f"KR:{self.content_sha1()}")
        # 같은 원문을 다시 수집하면 같은 id → DynamoDB에서 덮어쓰기(중복 없음)
        (again,) = ep.collect_docs([Country("KR", "한국")], energy_ctx(llm=StubLLM([GOOD_ENERGY])))
        assert again.id == doc.id

    @staticmethod
    def content_sha1() -> str:
        """energy_policy.py가 계산하는 것과 같은 원문 sha1 (ctx.extra["seen_hashes"] 값)."""
        import hashlib

        body = tu.html_to_text(ENERGY_TEXT, max_chars=ep.MAX_CHARS_PER_SOURCE)
        combined = f"### 출처: {ENERGY_URL}\n{body}"
        return hashlib.sha1(combined.encode("utf-8", "replace")).hexdigest()

    def test_reports_content_hash_and_doc_text(self, energy_ctx):
        # 수집기가 INGEST#energy_policy/LATEST.hashes와 S3 전문에 쓰는 두 출력
        ctx = energy_ctx(llm=StubLLM([GOOD_ENERGY]))
        (doc,) = ep.collect_docs([Country("KR", "한국")], ctx)
        assert ctx.extra["energy_hashes"] == {"KR": self.content_sha1()}
        assert doc.payload["content_sha1"] == self.content_sha1()
        text = ctx.extra["doc_texts"][f"energy_policy/KR/{doc.id}"]
        assert text.startswith(f"### 출처: {ENERGY_URL}")
        assert "32.9% by 2038" in text and "절단" not in text

    def test_doc_text_truncated_with_marker(self, monkeypatch):
        urls = ["https://energy.test/1", "https://energy.test/2", "https://energy.test/3"]
        monkeypatch.setattr(ep, "load_sources", lambda *a, **k: {"energy_policy": {"KR": {"sources": urls}}})
        long_html = (
            "<html><body><main><p>"
            + "Korea aims to raise the share of renewables in power generation to 32.9% by 2038. " * 200
            + "</p></main></body></html>"
        )
        ctx = StubCtx(pages=dict.fromkeys(urls, long_html), llm=StubLLM([GOOD_ENERGY]))
        (doc,) = ep.collect_docs([Country("KR", "한국")], ctx)
        text = ctx.extra["doc_texts"][f"energy_policy/KR/{doc.id}"]
        assert text.endswith("… (12,000자에서 절단)")
        assert len(text) <= common.DOC_TEXT_MAX_CHARS + 40

    def test_no_hash_or_text_reported_when_doc_is_discarded(self, energy_ctx):
        ctx = energy_ctx(llm=StubLLM([dict(GOOD_ENERGY, quotes=["존재하지 않는 인용문입니다"])]))
        assert ep.collect_docs([Country("KR", "한국")], ctx) == []
        # 문서를 못 만들면 해시를 남기지 않는다 → 다음 실행에서 다시 시도
        assert "energy_hashes" not in ctx.extra and "doc_texts" not in ctx.extra

    def test_seen_hashes_skips_llm(self, energy_ctx):
        llm = StubLLM([GOOD_ENERGY])
        ctx = energy_ctx(llm=llm, extra={"seen_hashes": {self.content_sha1()}})
        assert ep.collect_docs([Country("KR", "한국")], ctx) == []
        assert llm.calls == []
        assert any("원문 변경 없음" in m for m in ctx.logs)

    def test_no_llm_saves_raw_only(self, energy_ctx):
        ctx = energy_ctx(no_llm=True, llm=StubLLM([GOOD_ENERGY]))
        assert ep.collect_docs([Country("KR", "한국")], ctx) == []
        assert len(ctx.raw) == 1

    def test_quote_absent_discards_doc(self, energy_ctx):
        ctx = energy_ctx(llm=StubLLM([dict(GOOD_ENERGY, quotes=["존재하지 않는 인용문입니다"])]))
        assert ep.collect_docs([Country("KR", "한국")], ctx) == []
        assert any("인용문이 원문에 없어" in e for e in ctx.errors)

    def test_empty_targets_and_changes_discards_doc(self, energy_ctx):
        ctx = energy_ctx(llm=StubLLM([dict(GOOD_ENERGY, targets=[], recent_changes=[])]))
        assert ep.collect_docs([Country("KR", "한국")], ctx) == []
        assert any("모두 비어" in e for e in ctx.errors)

    def test_fetch_failure_is_isolated(self, energy_ctx):
        ctx = energy_ctx(llm=StubLLM([GOOD_ENERGY]), pages={})
        assert ep.collect_docs([Country("KR", "한국")], ctx) == []
        assert any("HTTP 404" in e for e in ctx.errors)


# ================================================================ weekly_brief
LATEST_ROWS = [
    {"iso": "KR", "indicator": "policy_rate", "value": 2.5, "unit": "%", "period": "2026-09-19",
     "change": -0.25},
    {"iso": "US", "indicator": "cpi_yoy", "value": 2.9, "unit": "%", "period": "2026-08"},
]
RECENT_DOCS = [
    {"type": "cb_stance", "iso": "KR", "date": "2026-09-11", "id": "abc123abc123",
     "title_ko": "한국은행 기준금리 인하", "summary_ko": "한국은행은 기준금리를 0.25%p 인하했다. 물가 둔화를 근거로 들었다.",
     "source_url": "https://www.bok.or.kr/x"},
    {"type": "poll", "iso": "GB", "date": "2026-09-18", "id": "def456def456",
     "title_ko": "영국 여론조사", "summary_ko": "리폼UK가 24%로 선두를 유지했다.",
     "source_url": "https://en.wikipedia.org/wiki/y"},
]
BRIEF_KEY_KR = "DOC#cb_stance#KR|2026-09-11#abc123abc123"
GOOD_BRIEF = {
    "title_ko": "G20 주간 브리프",
    "bullets": [f"불릿 {i}: 정책금리와 물가 흐름을 요약한다." for i in range(1, 7)],
    "evidence": [
        {"doc_key": BRIEF_KEY_KR, "url": "https://www.bok.or.kr/x"},
        {"doc_key": "DOC#cb_stance#ZZ|2026-01-01#000000000000", "url": "https://fake.example/z"},
    ],
    "confidence": 0.7,
}


class TestWeeklyBrief:
    def test_builds_doc_and_filters_hallucinated_evidence(self):
        llm = StubLLM([GOOD_BRIEF])
        ctx = StubCtx(llm=llm)
        doc = wb.build_brief(LATEST_ROWS, RECENT_DOCS, ctx)
        assert doc is not None
        assert (doc.type, doc.iso) == ("weekly_brief", "G20")
        assert doc.payload["week_start"] == "2026-09-14" or len(doc.payload["week_start"]) == 10
        assert len(doc.payload["bullets"]) == 6
        assert doc.payload["evidence"] == [
            {"doc_key": BRIEF_KEY_KR, "url": "https://www.bok.or.kr/x"}
        ]
        assert doc.quotes == ["한국은행은 기준금리를 0.25%p 인하했다."]
        assert doc.ai_generated is True and doc.review_status == "pending"
        assert doc.id == tu.sha12(f"weekly_brief:{doc.payload['week_start']}")
        user = llm.calls[0]["user"]
        assert BRIEF_KEY_KR in user and "policy_rate" in user
        assert llm.calls[0]["schema"] is bj.json_schema_brief

    def test_wrong_url_for_known_key_is_dropped(self):
        payload = dict(GOOD_BRIEF, evidence=[{"doc_key": BRIEF_KEY_KR, "url": "https://evil/x"}])
        doc = wb.build_brief(LATEST_ROWS, RECENT_DOCS, StubCtx(llm=StubLLM([payload])))
        assert doc is not None and doc.payload["evidence"] == []
        assert doc.quotes  # 근거가 비면 입력 문서 앞쪽에서 인용을 만든다

    def test_too_few_bullets_is_rejected(self):
        payload = dict(GOOD_BRIEF, bullets=["하나", "둘"])
        ctx = StubCtx(llm=StubLLM([payload]))
        assert wb.build_brief(LATEST_ROWS, RECENT_DOCS, ctx) is None
        assert any("불릿이" in e for e in ctx.errors)

    def test_no_llm_returns_none(self):
        llm = StubLLM([GOOD_BRIEF])
        assert wb.build_brief(LATEST_ROWS, RECENT_DOCS, StubCtx(llm=llm, no_llm=True)) is None
        assert llm.calls == []

    def test_empty_input_returns_none(self):
        assert wb.build_brief([], [], StubCtx(llm=StubLLM([GOOD_BRIEF]))) is None

    def test_llm_failure_returns_none(self):
        ctx = StubCtx(llm=StubLLM([RuntimeError("bedrock 오류")]))
        assert wb.build_brief(LATEST_ROWS, RECENT_DOCS, ctx) is None
        assert any("LLM 브리프 실패" in e for e in ctx.errors)

    def test_docs_without_summary_are_rejected_for_quotes(self):
        docs = [dict(d, summary_ko="", title_ko="") for d in RECENT_DOCS]
        payload = dict(GOOD_BRIEF, evidence=[])
        ctx = StubCtx(llm=StubLLM([payload]))
        assert wb.build_brief(LATEST_ROWS, docs, ctx) is None
        assert any("quotes 필수" in e for e in ctx.errors)


# ================================================================ sources.yaml
class TestSourcesYaml:
    """sources.yaml은 이 계층의 계약 데이터다 — 구조가 깨지면 전부 조용히 실패한다."""

    @pytest.fixture(scope="class")
    def data(self):
        return load_sources()

    def test_sections_exist(self, data):
        assert set(data) >= {"central_banks", "polls", "energy_policy"}

    def test_polls_wiki_pages_are_titles_not_urls(self, data):
        for iso, meta in data["polls"].items():
            page = meta.get("wiki_page")
            if page:
                assert " " not in str(page), iso  # 밑줄 형태 제목이어야 한다
                assert not str(page).startswith("http"), iso

    def test_central_banks_cover_all_countries(self, data):
        cbs = data["central_banks"]
        assert len(cbs) == 20
        for iso, meta in cbs.items():
            assert meta.get("name_ko"), iso
            if meta.get("refer"):
                assert meta["refer"] == "EU", iso
                continue
            assert str(meta.get("statements_url", "")).startswith("http"), iso
            assert meta.get("lang"), iso
        assert {"DE", "FR", "IT"} <= {i for i, m in cbs.items() if m.get("refer") == "EU"}

    def test_polls_cover_all_twenty_entries(self, data):
        """20 엔트리 = 여론조사 대상 17개국 + not_applicable 3개(CN·SA·EU)."""
        polls = data["polls"]
        assert len(polls) == 20
        for iso in ("CN", "SA", "EU"):
            assert polls[iso].get("not_applicable_reason"), iso
            assert not polls[iso].get("wiki_page"), iso
        actionable = [i for i in polls if not polls[i].get("not_applicable_reason")]
        assert len(actionable) == 17
        for iso in actionable:
            meta = polls[iso]
            # wiki_page가 null이면 왜 못 찾았는지 note로 남겨야 한다 (추측 금지)
            assert meta.get("wiki_page") or meta.get("wiki_page_note"), iso
            assert meta.get("trust") in ("높음", "보통", "낮음"), iso
            aliases = meta.get("party_aliases") or {}
            assert len(aliases) >= 2, iso
            for ko, names in aliases.items():
                assert isinstance(names, list) and names, f"{iso}/{ko}"
            election = meta.get("election") or {}
            assert set(election) >= {
                "next_election_date", "election_type", "ruling_party_ko",
                "ruling_lean", "second_party_ko", "system_note",
            }, iso
            if election["next_election_date"] is None:
                assert election.get("note"), iso  # 확인 못 한 값은 note로 정직하게 표기
            else:
                assert len(str(election["next_election_date"])) in (7, 10), iso

    def test_alias_map_is_unambiguous_per_country(self, data):
        for iso, meta in data["polls"].items():
            aliases = meta.get("party_aliases") or {}
            amap = pw.build_alias_map(aliases)
            assert len(amap) >= len(aliases), iso

    def test_energy_policy_urls(self, data):
        energy = data["energy_policy"]
        assert len(energy) == 20
        for iso, meta in energy.items():
            urls = meta.get("sources") or []
            assert urls, iso
            assert all(str(u).startswith("http") for u in urls), iso


# ================================================================ 통합(선택)
class TestRealCollectContext:
    """실제 CollectContext/Country와의 호환 확인.

    sources/base.py·registry.py는 정량 소스 담당 소유이므로 아직 없을 수 있다 →
    importorskip으로 건너뛴다(이 파일은 그 두 모듈 없이도 전부 통과해야 한다).
    """

    @pytest.fixture
    def real(self):
        base = pytest.importorskip("macro.sources.base")
        registry = pytest.importorskip("macro.registry")
        return base.CollectContext, registry.Country

    def test_polls_run_on_real_context_without_llm(self, real):
        CollectContext, RealCountry = real
        # 실제 sources.yaml을 그대로 쓴다 (monkeypatch 없음) → 계약 데이터까지 검증
        url = pw.WIKI_BASE + load_sources()["polls"]["KR"]["wiki_page"]

        class Resp:
            def __init__(self, text):
                self.text, self.status_code = text, 200
                self.content = text.encode()

        class Session:
            def __init__(self, pages):
                self.pages, self.seen = pages, []

            def get(self, url, params=None, headers=None, timeout=None):
                self.seen.append(url)
                return Resp(self.pages.get(url, ""))

        ctx = CollectContext(
            dry_run=True, no_llm=True,
            http=Session({url: fixture("wiki_polls_kr.html")}), extra={},
        )
        kr = RealCountry(
            iso="KR", iso3="KOR", name_ko="한국", ccy="KRW", groups=["G20"],
            euro=False, codes={},
        )
        docs = pw.collect_docs([kr], ctx)
        assert {d.type for d in docs} == {"poll", "election"}
        obs = pw.collect([kr], [], ctx)
        assert all(o.indicator in ("party_support", "gov_approval") for o in obs)
        assert docs[0].to_item()["pk"].startswith("DOC#")
        assert obs and obs[0].to_item()["sk"].startswith("W#")

    def test_euro_member_skipped_on_real_context(self, real):
        CollectContext, RealCountry = real

        class Session:
            seen: list = []

            def get(self, *a, **k):
                raise AssertionError("유로 회원국은 네트워크 호출을 하지 않아야 한다")

        ctx = CollectContext(dry_run=True, no_llm=True, http=Session(), extra={})
        de = RealCountry(
            iso="DE", iso3="DEU", name_ko="독일", ccy="EUR", groups=["G20", "EU"],
            euro=True, codes={},
        )
        assert cb.collect_docs([de], ctx) == []
