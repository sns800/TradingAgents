# [모듈 개요] 티커(ticker) 심볼 입력 처리를 검증하는 테스트.
# CLI 입력 정규화(대문자화, 공백 제거)와 거래소 접미사 유지,
# 상품 컨텍스트 문구 생성, get_ticker 중복 정의 방지를 다룬다.

import unittest

import pytest

from cli.utils import normalize_ticker_symbol
from tradingagents.agents.utils.agent_utils import build_instrument_context


@pytest.mark.unit
class TickerSymbolHandlingTests(unittest.TestCase):
    def test_normalize_ticker_symbol_preserves_exchange_suffix(self):
        """티커 정규화가 거래소 접미사(.TO 등)를 유지한 채 대문자화하는지 검증하는 테스트."""
        self.assertEqual(normalize_ticker_symbol(" cnc.to "), "CNC.TO")

    def test_build_instrument_context_mentions_exact_symbol(self):
        """상품 컨텍스트 문구에 정확한 심볼과 거래소 접미사 언급이 포함되는지 검증하는 테스트."""
        context = build_instrument_context("7203.T")
        self.assertIn("7203.T", context)
        self.assertIn("exchange suffix", context)

    def test_single_get_ticker_no_shadow(self):
        """get_ticker가 중복 정의 없이 단일 정의를 공유하는지 검증하는 테스트."""
        # 회귀(regression) 방지: cli/main.py에 빈 questionary 프롬프트(화면에
        # "?"만 표시됨)를 가진 중복 get_ticker가 있어 cli/utils의 설명이 담긴
        # 정의를 가렸다. 단일 표준(canonical) 정의만 유지한다.
        import cli.main
        import cli.utils
        self.assertIs(cli.main.get_ticker, cli.utils.get_ticker)


if __name__ == "__main__":
    unittest.main()
