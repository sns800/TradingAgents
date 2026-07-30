# =============================================================================
# [모듈 개요 - 초보자용 안내]
# 이 파일은 dataflows 패키지의 전역 설정(configuration)을 관리하는 모듈입니다.
# 기본 설정(DEFAULT_CONFIG)을 불러오고, 사용자가 일부 값만 덮어쓸 수 있게 합니다.
# TradingAgents(LLM 멀티 에이전트 주식 트레이딩 프레임워크) 전반에서 어떤 데이터
# 벤더(vendor, 데이터 공급자)를 쓸지 등의 설정을 이 모듈을 통해 읽고 변경합니다.
# =============================================================================
from copy import deepcopy

import tradingagents.default_config as default_config

# 기본 설정을 사용하되, 나중에 덮어쓸(override) 수 있도록 모듈 전역 변수로 둔다
_config: dict | None = None


def initialize_config():
    """설정을 기본값으로 초기화한다."""
    global _config
    if _config is None:
        _config = deepcopy(default_config.DEFAULT_CONFIG)


def set_config(config: dict):
    """사용자 지정 값으로 설정을 갱신한다.

    딕셔너리 값을 가진 키(예: ``data_vendors``)는 한 단계 깊이까지 병합(merge)되므로,
    ``{"data_vendors": {"core_stock_apis": "alpha_vantage"}}`` 처럼 일부만 지정해도
    기본값의 나머지 중첩 키들이 유지된다. 스칼라(단일 값) 키는 통째로 교체된다.
    """
    global _config
    initialize_config()
    incoming = deepcopy(config)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(_config.get(key), dict):
            _config[key].update(value)
        else:
            _config[key] = value


def get_config() -> dict:
    """현재 설정을 반환한다."""
    if _config is None:
        initialize_config()
    # deepcopy로 복사본을 반환해, 호출자가 반환값을 수정해도
    # 내부 전역 설정이 오염되지 않게 한다
    return deepcopy(_config)


# 모듈 임포트 시점에 기본 설정으로 초기화
initialize_config()
