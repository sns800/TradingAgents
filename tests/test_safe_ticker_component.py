"""[모듈 개요] 디렉터리 순회(directory traversal) 공격을 차단하는
티커(ticker) 경로 구성 요소 검증기(validator)를 검증하는 테스트.
"""

import os
import unittest

import pytest

from tradingagents.dataflows.utils import safe_ticker_component


@pytest.mark.unit
class TestSafeTickerComponent(unittest.TestCase):
    def test_accepts_common_ticker_formats(self):
        """일반적인 티커 형식(미국/해외 거래소, 지수 등)이 그대로 허용되는지 검증하는 테스트."""
        for ticker in ("AAPL", "BRK-B", "BRK.A", "0700.HK", "7203.T", "BHP.AX", "^GSPC"):
            self.assertEqual(safe_ticker_component(ticker), ticker)

    def test_accepts_futures_and_forex_formats(self):
        """선물(futures)과 외환(forex) 티커 형식이 허용되는지 검증하는 테스트."""
        # 선물은 '='를 쓰고(GC=F 금, CL=F 원유), 외환/CFD 심볼은 '+'를 쓴다.
        for ticker in ("GC=F", "CL=F", "ES=F", "XAUUSD+", "EURUSD+"):
            self.assertEqual(safe_ticker_component(ticker), ticker)

    def test_rejects_path_separators(self):
        """경로 구분자가 포함된 입력이 거부되는지 검증하는 테스트."""
        for bad in (".", "..", "../etc", "a/b", "a\\b", "/abs", "..\\..\\x"):
            with self.assertRaises(ValueError):
                safe_ticker_component(bad)

    def test_rejects_null_byte_and_whitespace(self):
        """널 바이트(null byte)나 공백 문자가 포함된 입력이 거부되는지 검증하는 테스트."""
        for bad in ("AAP L", "AAPL\x00", "AAPL\n", "\tAAPL"):
            with self.assertRaises(ValueError):
                safe_ticker_component(bad)

    def test_rejects_empty_or_non_string(self):
        """빈 값이나 문자열이 아닌 입력이 거부되는지 검증하는 테스트."""
        for bad in ("", None, 123, b"AAPL"):
            with self.assertRaises(ValueError):
                safe_ticker_component(bad)

    def test_rejects_overlong_input(self):
        """지나치게 긴 입력이 거부되는지 검증하는 테스트."""
        with self.assertRaises(ValueError):
            safe_ticker_component("A" * 33)

    def test_rejects_dot_only_values(self):
        """점(.)으로만 이루어진 값이 거부되는지 검증하는 테스트."""
        # '.'과 '..'은 정규식은 통과하지만 경로 구성 요소로 쓰이면 상위 디렉터리로
        # 이동(traverse)한다 (예: ``Path(results_dir) / ticker / "logs"``).
        for bad in (".", "..", "...", "...."):
            with self.assertRaises(ValueError):
                safe_ticker_component(bad)

    def test_traversal_string_does_not_escape_join(self):
        """정합성 확인(sanity check): 정제된 값이 경로 결합 후에도 기준 디렉터리를 벗어나지 않는지 검증하는 테스트."""
        base = os.path.realpath("/tmp/cache")
        ticker = safe_ticker_component("AAPL")
        joined = os.path.realpath(os.path.join(base, f"{ticker}.csv"))
        self.assertTrue(joined.startswith(base + os.sep))


if __name__ == "__main__":
    unittest.main()
