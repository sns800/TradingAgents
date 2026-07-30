# 이 파일은 dataflows 전역 설정(config)의 get/set 함수가 내부 dict 참조를
# 외부로 누출하지 않는지(격리, isolation) 검증하는 테스트 모음입니다.
"""설정 격리(isolation) 테스트: get/set이 중첩 dict의 참조를 누출하면 안 됩니다."""

import copy
import unittest

import pytest

import tradingagents.default_config as default_config
from tradingagents.dataflows.config import get_config, set_config


@pytest.mark.unit
class DataflowsConfigIsolationTests(unittest.TestCase):
    def setUp(self):
        set_config(copy.deepcopy(default_config.DEFAULT_CONFIG))

    def test_get_config_returns_deep_copy(self):
        """get_config가 깊은 복사본(deep copy)을 반환해 수정이 원본에 영향을 주지 않는지 검증하는 테스트."""
        cfg = get_config()
        cfg["data_vendors"]["core_stock_apis"] = "alpha_vantage"
        cfg["tool_vendors"]["get_stock_data"] = "alpha_vantage"

        fresh = get_config()
        self.assertEqual(fresh["data_vendors"]["core_stock_apis"], "yfinance")
        self.assertNotIn("get_stock_data", fresh["tool_vendors"])

    def test_set_config_does_not_alias_caller_nested_dicts(self):
        """set_config 이후 호출자의 dict를 수정해도 저장된 설정이 바뀌지 않는지 검증하는 테스트."""
        custom = copy.deepcopy(default_config.DEFAULT_CONFIG)
        custom["data_vendors"]["core_stock_apis"] = "alpha_vantage"
        custom["tool_vendors"]["get_stock_data"] = "alpha_vantage"

        set_config(custom)

        custom["data_vendors"]["core_stock_apis"] = "yfinance"
        custom["tool_vendors"]["get_stock_data"] = "yfinance"

        fresh = get_config()
        self.assertEqual(fresh["data_vendors"]["core_stock_apis"], "alpha_vantage")
        self.assertEqual(fresh["tool_vendors"]["get_stock_data"], "alpha_vantage")

    def test_partial_nested_update_preserves_existing_defaults(self):
        """중첩 dict의 일부만 갱신해도 나머지 기본값이 보존되는지 검증하는 테스트."""
        set_config(
            {
                "data_vendors": {
                    "core_stock_apis": "alpha_vantage",
                }
            }
        )

        fresh = get_config()
        self.assertEqual(fresh["data_vendors"]["core_stock_apis"], "alpha_vantage")
        self.assertEqual(fresh["data_vendors"]["technical_indicators"], "yfinance")
        self.assertEqual(fresh["data_vendors"]["fundamental_data"], "yfinance")
        self.assertEqual(fresh["data_vendors"]["news_data"], "yfinance")

    def test_nested_dict_updates_merge_one_level_deep(self):
        """연속된 set_config 호출이 중첩 dict를 한 단계 깊이까지 병합(merge)하는지 검증하는 테스트."""
        set_config({"tool_vendors": {"get_stock_data": "alpha_vantage"}})
        set_config({"tool_vendors": {"get_news": "alpha_vantage"}})

        fresh = get_config()
        self.assertEqual(fresh["tool_vendors"]["get_stock_data"], "alpha_vantage")
        self.assertEqual(fresh["tool_vendors"]["get_news"], "alpha_vantage")
