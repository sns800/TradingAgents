"""[모듈 개요] 뉴스 분석가(news analyst) 프롬프트가 실제 도구 시그니처(tool signature)와
어긋나지 않도록 지키는 테스트 (#1116).

과거에는 프롬프트가 ``get_news(query, ...)`` 형태를 안내했지만 실제 도구는
``ticker``를 받기 때문에, LLM이 자유 텍스트 쿼리 호출을 지어내는(hallucinate)
문제가 있었다.
"""
import inspect

import pytest

import tradingagents.agents.analysts.news_analyst as na
from tradingagents.agents.utils.news_data_tools import get_news


@pytest.mark.unit
def test_get_news_takes_ticker_not_query():
    """get_news 도구가 query가 아닌 ticker 인자를 받는지 검증하는 테스트."""
    arg_names = set(get_news.args.keys())
    assert "ticker" in arg_names
    assert "query" not in arg_names


@pytest.mark.unit
def test_news_prompt_matches_get_news_signature():
    """뉴스 분석가 프롬프트에 적힌 get_news 사용 예시가 실제 시그니처와 일치하는지 검증하는 테스트."""
    src = inspect.getsource(na)
    assert "get_news(ticker, start_date, end_date)" in src
    assert "get_news(query" not in src
