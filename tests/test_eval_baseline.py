# 이 파일은 단일 LLM 베이스라인을 검증하는 테스트 모음입니다. LLM과 데이터
# 벤더 호출을 전부 모킹해, 프롬프트 구성(티커·날짜·데이터 포함, 조회 범위가
# trade_date에서 끝나는지), "Rating: X" 형식 강제와 등급 추출, 데이터 실패
# 내성, LLM 팩토리 연동을 결정론적으로 확인합니다.
"""단일 LLM 베이스라인 테스트 — 프롬프트 구성, 등급 추출, 실패 내성."""

import copy
from unittest.mock import MagicMock, patch

import pytest

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.eval.baseline import (
    BASELINE_SYSTEM_PROMPT,
    MAX_SECTION_CHARS,
    build_baseline_context,
    build_baseline_messages,
    run_single_llm_baseline,
)

# ---------------------------------------------------------------------------
# 공용 헬퍼
# ---------------------------------------------------------------------------


def _fake_llm(content):
    """고정된 응답 텍스트를 반환하는 가짜 LLM을 만든다."""
    llm = MagicMock()
    llm.invoke.return_value = MagicMock(content=content)
    return llm


def _fake_vendor(price="PRICE_TABLE", news="NEWS_HEADLINES"):
    """route_to_vendor 대체용 — 호출을 기록하고 고정 문자열을 반환한다."""
    calls = []

    def vendor(method, *args):
        calls.append((method, *args))
        if method == "get_stock_data":
            return price
        if method == "get_news":
            return news
        raise AssertionError(f"unexpected vendor method: {method}")

    return vendor, calls


# ---------------------------------------------------------------------------
# 컨텍스트 구성 — 조회 범위와 내용
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_context_fetches_end_at_trade_date():
    vendor, calls = _fake_vendor()
    with patch("tradingagents.eval.baseline.route_to_vendor", side_effect=vendor):
        context = build_baseline_context("AAPL", "2025-01-06")

    methods = {c[0]: c for c in calls}
    # 주가: 30일 되돌아보기, 뉴스: 7일 되돌아보기 — 둘 다 trade_date에서 끝남
    assert methods["get_stock_data"] == ("get_stock_data", "AAPL", "2024-12-07", "2025-01-06")
    assert methods["get_news"] == ("get_news", "AAPL", "2024-12-30", "2025-01-06")
    # 룩어헤드 방지: 어떤 조회도 trade_date 이후로 끝나지 않아야 한다
    for call in calls:
        assert call[-1] <= "2025-01-06"

    assert "PRICE_TABLE" in context
    assert "NEWS_HEADLINES" in context


@pytest.mark.unit
def test_context_survives_vendor_failure():
    def broken_vendor(method, *args):
        raise ConnectionError("vendor down")

    with patch("tradingagents.eval.baseline.route_to_vendor", side_effect=broken_vendor):
        context = build_baseline_context("AAPL", "2025-01-06")

    # 실패는 섹션에 표시로 남고 예외는 전파되지 않는다
    assert "unavailable" in context
    assert "ConnectionError" in context


@pytest.mark.unit
def test_context_sections_are_truncated():
    vendor, _ = _fake_vendor(price="p" * (MAX_SECTION_CHARS * 3))
    with patch("tradingagents.eval.baseline.route_to_vendor", side_effect=vendor):
        context = build_baseline_context("AAPL", "2025-01-06")
    assert "(truncated)" in context
    assert len(context) < MAX_SECTION_CHARS * 2


# ---------------------------------------------------------------------------
# 프롬프트 구성 — "Rating: X" 형식 강제
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_system_prompt_forces_rating_line():
    assert "Rating:" in BASELINE_SYSTEM_PROMPT
    for rating in ("Buy", "Overweight", "Hold", "Underweight", "Sell"):
        assert rating in BASELINE_SYSTEM_PROMPT


@pytest.mark.unit
def test_messages_include_ticker_date_and_context():
    messages = build_baseline_messages("AAPL", "2025-01-06", "CONTEXT_BLOB")
    assert messages[0] == ("system", BASELINE_SYSTEM_PROMPT)
    role, human = messages[1]
    assert role == "human"
    assert "Ticker: AAPL" in human
    assert "Analysis date: 2025-01-06" in human
    assert "CONTEXT_BLOB" in human


# ---------------------------------------------------------------------------
# run_single_llm_baseline — 등급 추출과 LLM 연동
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_run_extracts_rating_from_response():
    vendor, _ = _fake_vendor()
    llm = _fake_llm("Momentum is strong and news flow is positive.\nRating: Overweight")
    with patch("tradingagents.eval.baseline.route_to_vendor", side_effect=vendor):
        out = run_single_llm_baseline("AAPL", "2025-01-06", llm=llm)

    assert out["rating"] == "Overweight"
    assert out["ticker"] == "AAPL"
    assert out["trade_date"] == "2025-01-06"
    assert "Rating: Overweight" in out["response"]

    # LLM에 전달된 메시지에 시스템 프롬프트와 수집 데이터가 들어 있어야 한다
    (messages,) = llm.invoke.call_args.args
    assert messages[0] == ("system", BASELINE_SYSTEM_PROMPT)
    assert "PRICE_TABLE" in messages[1][1]
    assert "NEWS_HEADLINES" in messages[1][1]


@pytest.mark.unit
def test_run_defaults_to_hold_when_no_rating():
    vendor, _ = _fake_vendor()
    llm = _fake_llm("I cannot decide based on this data.")
    with patch("tradingagents.eval.baseline.route_to_vendor", side_effect=vendor):
        out = run_single_llm_baseline("AAPL", "2025-01-06", llm=llm)
    assert out["rating"] == "Hold"


@pytest.mark.unit
def test_run_creates_llm_from_config_when_not_injected():
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["llm_provider"] = "openai"
    config["deep_think_llm"] = "gpt-test-deep"

    vendor, _ = _fake_vendor()
    factory = MagicMock()
    factory.return_value.get_llm.return_value = _fake_llm("Rating: Buy")
    with (
        patch("tradingagents.eval.baseline.route_to_vendor", side_effect=vendor),
        patch("tradingagents.eval.baseline.create_llm_client", factory),
    ):
        out = run_single_llm_baseline("AAPL", "2025-01-06", config=config)

    assert out["rating"] == "Buy"
    # 깊은 사고용(deep_think_llm) 모델로 팩토리가 호출되어야 한다
    kwargs = factory.call_args.kwargs
    assert kwargs["provider"] == "openai"
    assert kwargs["model"] == "gpt-test-deep"
