# 이 파일은 FRED(미국 연준 경제 데이터) 거시경제 지표 벤더를 검증하는
# 테스트 모음입니다. 별칭(alias) 해석, 설정 오류, 출력 형식, 결측값 처리,
# 미래 데이터 차단 윈도우, 라우터 연동을 확인합니다.
"""FRED 거시경제(macro) 벤더 테스트: 별칭(alias) 해석, 설정 오류, 출력 형식,
결측값 처리, 미래 데이터 차단(lookahead-safe) 윈도우, 라우터 연동.

모든 API 접근은 모킹(mock)되어 있어 네트워크 연결이나 키 없이 실행됩니다.
"""
import copy
import unittest
from unittest import mock

import pytest

import tradingagents.dataflows.config as config_module
import tradingagents.default_config as default_config
from tradingagents.dataflows import fred, interface
from tradingagents.dataflows.config import set_config

# 출력 형식을 검증하기 위한 작고 안정적인 관측값(observation) 집합.
_META = {
    "seriess": [
        {
            "title": "Unemployment Rate",
            "units_short": "%",
            "frequency": "Monthly",
            "seasonal_adjustment_short": "SA",
        }
    ]
}
_OBS = {
    "observations": [
        {"date": "2025-06-01", "value": "4.1"},
        {"date": "2025-07-01", "value": "4.3"},
        {"date": "2025-08-01", "value": "."},   # 결측값 -> 건너뛰어야 함
        {"date": "2025-09-01", "value": "4.4"},
    ]
}


def _request_stub(meta=_META, obs=_OBS):
    """엔드포인트 경로에 따라 분기하는 _request 대체 함수를 만드는 헬퍼."""
    def _impl(path, params):
        if path == "series":
            return meta
        if path == "series/observations":
            return obs
        raise AssertionError(f"unexpected FRED path: {path}")
    return _impl


@pytest.mark.unit
class FredResolutionTests(unittest.TestCase):
    """지표 이름/별칭을 FRED 시리즈 ID로 해석하는 로직을 검증하는 테스트 묶음."""

    def test_alias_maps_to_series_id(self):
        """친숙한 별칭(cpi 등)이 올바른 FRED 시리즈 ID로 매핑되는지 검증하는 테스트."""
        self.assertEqual(fred._resolve_series_id("cpi"), "CPIAUCSL")
        self.assertEqual(fred._resolve_series_id("unemployment"), "UNRATE")

    def test_alias_is_case_and_separator_insensitive(self):
        """별칭이 대소문자와 구분자(공백/하이픈)에 무관하게 인식되는지 검증하는 테스트."""
        self.assertEqual(fred._resolve_series_id("Fed Funds Rate"), "FEDFUNDS")
        self.assertEqual(fred._resolve_series_id("10y-treasury"), "DGS10")

    def test_unknown_alias_is_treated_as_raw_series_id(self):
        """모르는 별칭은 원시(raw) 시리즈 ID로 간주하는지 검증하는 테스트."""
        # 숙련 사용자는 임의의 FRED 시리즈 ID를 넘길 수 있으며, 관례상 대문자화합니다.
        self.assertEqual(fred._resolve_series_id("dgs30"), "DGS30")
        self.assertEqual(fred._resolve_series_id("MyCustomSeries"), "MYCUSTOMSERIES")

    def test_descriptive_phrase_is_rejected(self):
        """서술형 문구는 시리즈 ID가 아니므로 거부되는지 검증하는 테스트."""
        # LLM이 만든 문구(공백 포함 / 너무 긺)는 시리즈 ID가 아닙니다 —
        # API에서 400 오류를 받는 대신 안내와 함께 사전에 거부합니다.
        for bad in ("bank of japan rate", "the unemployment number", "X" * 31):
            with self.assertRaises(ValueError):
                fred._resolve_series_id(bad)

    def test_get_macro_data_returns_guidance_on_bad_indicator(self):
        """잘못된 지표 이름에는 크래시 대신 안내 메시지를 반환하는지 검증하는 테스트."""
        # 유효하지 않은 지표 -> 크래시가 아닌 조치 가능한 메시지 (API 호출 없음).
        out = fred.get_macro_data("bank of japan rate", "2026-01-01")
        self.assertIn("FRED", out)
        self.assertIn("not a known macro alias", out)


@pytest.mark.unit
class FredConfigTests(unittest.TestCase):
    """FRED API 키 설정 관련 오류 처리를 검증하는 테스트 묶음."""

    def test_missing_key_raises_not_configured(self):
        """API 키가 없으면 FredNotConfiguredError가 발생하는지 검증하는 테스트."""
        with mock.patch.dict("os.environ", {}, clear=True), \
                self.assertRaises(fred.FredNotConfiguredError):
            fred.get_api_key()

    def test_not_configured_is_a_value_error(self):
        """FredNotConfiguredError가 ValueError의 하위 클래스인지 검증하는 테스트."""
        # 라우팅은 "벤더 사용 불가" 처리를 위해 이 상속 관계에 의존합니다.
        self.assertTrue(issubclass(fred.FredNotConfiguredError, ValueError))


@pytest.mark.unit
class FredFormattingTests(unittest.TestCase):
    """FRED 보고서 출력 형식을 검증하는 테스트 묶음."""

    def test_report_has_header_latest_change_and_table(self):
        """보고서에 헤더, 최신 값, 변동폭, 표가 모두 포함되는지 검증하는 테스트."""
        with mock.patch.object(fred, "_request", side_effect=_request_stub()):
            out = fred.get_macro_data("unemployment", "2025-09-30", 365)
        self.assertIn("## FRED: Unemployment Rate (UNRATE)", out)
        self.assertIn("Units: %", out)
        self.assertIn("Frequency: Monthly (SA)", out)
        self.assertIn("**Latest:** 4.4 (2025-09-01)", out)
        # 기간 내 변동폭: 4.4 - 4.1 = +0.30
        self.assertIn("+0.30", out)
        self.assertIn("| 2025-06-01 | 4.1 |", out)

    def test_missing_value_is_skipped(self):
        """결측값(".") 관측치가 표의 행으로 나타나지 않는지 검증하는 테스트."""
        with mock.patch.object(fred, "_request", side_effect=_request_stub()):
            out = fred.get_macro_data("unemployment", "2025-09-30", 365)
        # "." 관측값은 행으로 나타나면 안 됩니다
        self.assertNotIn("2025-08-01", out)

    def test_empty_window_reports_no_observations(self):
        """조회 기간에 관측값이 없으면 그 사실을 알리는 메시지를 내는지 검증하는 테스트."""
        empty = {"observations": []}
        with mock.patch.object(fred, "_request", side_effect=_request_stub(obs=empty)):
            out = fred.get_macro_data("unemployment", "2025-09-30", 30)
        self.assertIn("No observations", out)

    def test_unknown_series_returns_not_found_message(self):
        """형식은 맞지만 존재하지 않는 시리즈 ID에 안내 메시지를 반환하는지 검증하는 테스트."""
        # 형식은 올바르지만 알 수 없는 시리즈 ID는 크래시 대신 안내를 반환하여,
        # 선택 사항인 거시 지표 조회 때문에 전체 실행이 중단되지 않게 합니다.
        no_series = {"seriess": []}
        with mock.patch.object(fred, "_request", side_effect=_request_stub(meta=no_series)):
            out = fred.get_macro_data("totally_unknown_xyz", "2025-09-30", 30)
        self.assertIn("not found", out)

    def test_long_series_is_truncated_but_change_uses_full_range(self):
        """긴 시리즈는 표가 잘리되 변동폭 계산은 전체 범위를 쓰는지 검증하는 테스트."""
        # MAX_ROWS보다 많은 관측값을 결정론적으로 생성합니다.
        obs = {
            "observations": [
                {"date": f"2025-01-{(i % 28) + 1:02d}", "value": str(i)}
                for i in range(fred.MAX_ROWS + 10)
            ]
        }
        with mock.patch.object(fred, "_request", side_effect=_request_stub(obs=obs)):
            out = fred.get_macro_data("unemployment", "2025-12-31", 365)
        self.assertIn(f"most recent {fred.MAX_ROWS}", out)
        # 기간 내 변동폭은 실제 첫 값(0)과 마지막 값을 기준으로 해야 합니다
        self.assertIn("from 0 ", out)
        body_rows = [ln for ln in out.splitlines() if ln.startswith("| 2025")]
        self.assertEqual(len(body_rows), fred.MAX_ROWS)

    def test_window_is_lookahead_safe(self):
        """과거 날짜 조회 시 미래 데이터가 섞이지 않는지 검증하는 테스트."""
        # observation_end가 curr_date와 같아야 과거 날짜 조회가 미래 데이터를 끌어오지 않습니다.
        captured = {}

        def _capture(path, params):
            captured[path] = params
            return _META if path == "series" else _OBS

        with mock.patch.object(fred, "_request", side_effect=_capture):
            fred.get_macro_data("unemployment", "2025-09-30", 90)
        obs_params = captured["series/observations"]
        self.assertEqual(obs_params["observation_end"], "2025-09-30")
        self.assertEqual(obs_params["observation_start"], "2025-07-02")  # 90일 전


@pytest.mark.unit
class FredRoutingTests(unittest.TestCase):
    """거시 지표 요청이 FRED 벤더로 올바르게 라우팅되는지 검증하는 테스트 묶음."""

    def setUp(self):
        config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)

    def tearDown(self):
        config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)

    def test_macro_category_routes_to_fred(self):
        """macro_data 카테고리 요청이 FRED 구현으로 라우팅되는지 검증하는 테스트."""
        self.assertEqual(
            interface.get_category_for_method("get_macro_indicators"), "macro_data"
        )
        set_config({"data_vendors": {"macro_data": "fred"}})
        with mock.patch.dict(
            interface.VENDOR_METHODS,
            {"get_macro_indicators": {"fred": lambda *a, **k: "MACRO_OK"}},
            clear=False,
        ):
            out = interface.route_to_vendor("get_macro_indicators", "cpi", "2026-06-01", 365)
        self.assertEqual(out, "MACRO_OK")

    def test_not_configured_degrades_gracefully(self):
        """FRED 키가 없어도 실행이 중단되지 않고 우아하게 저하(degrade)되는지 검증하는 테스트."""
        # macro_data는 선택 사항입니다: fred만 설정되고 키가 없으면 라우터는
        # 실행을 중단하는 대신 감시값(sentinel)으로 저하됩니다 — 선택적 키가
        # 없다고 분석이 크래시되면 안 됩니다.
        set_config({"data_vendors": {"macro_data": "fred"}})

        def _unconfigured(*a, **k):
            raise fred.FredNotConfiguredError("FRED_API_KEY not set")

        with mock.patch.dict(
            interface.VENDOR_METHODS,
            {"get_macro_indicators": {"fred": _unconfigured}},
            clear=False,
        ):
            out = interface.route_to_vendor("get_macro_indicators", "cpi", "2026-06-01", 365)
        self.assertIn("DATA_UNAVAILABLE", out)


if __name__ == "__main__":
    unittest.main()
