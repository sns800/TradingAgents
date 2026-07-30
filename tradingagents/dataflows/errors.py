# =============================================================================
# [모듈 개요 - 초보자용 안내]
# 이 파일은 데이터 벤더(vendor, 데이터 공급자) 관련 오류를 분류하는 예외(exception)
# 클래스들을 정의합니다. "데이터 없음", "요청 한도 초과", "API 키 미설정" 같은
# 상황을 구분해, TradingAgents(LLM 멀티 에이전트 주식 트레이딩 프레임워크)의
# 라우팅 계층이 상황에 맞게(다른 벤더로 전환 등) 대응할 수 있게 합니다.
# =============================================================================
"""벤더 데이터 오류 분류 체계(taxonomy).

라우팅 계층이 벤더별이 아니라 *행동(behavior)* 기준으로 반응할 수 있도록 단일
계층 구조로 구성했다: 벤더가 사용 가능한 데이터를 반환하지 못하는 모든 상황은
``VendorError`` 를 상속하고, 라우터는 이 기반(base) 타입들만 잡는다. 새 벤더는
이 예외들(또는 벤더 이름을 붙인 얇은 서브클래스)을 발생시키기만 하면 되고,
새로운 ``except`` 절을 추가할 필요가 없다.

    VendorError
    ├── NoMarketDataError          사용 가능한 행이 없음 (빈 결과 또는 오래된 데이터)
    ├── VendorRateLimitError       일시적 요청 제한(throttle) -> 다음 벤더로 넘어감
    └── VendorNotConfiguredError   API 키/설정 누락 -> 벤더 사용 불가

예외 타입의 개수는 사람이 설명할 수 있는 원인의 개수가 아니라, 라우터가 서로
다르게 반응해야 하는 경우의 수다: 빈 데이터와 오래된(stale) 데이터는 동일하게
처리되므로 ``NoMarketDataError`` 하나를 공유하고, 자유 텍스트 ``detail`` 로만
구분한다.
"""

from __future__ import annotations


class VendorError(Exception):
    """벤더가 사용 가능한 데이터를 반환하지 못한 모든 상황의 기반(base) 예외."""


class NoMarketDataError(VendorError):
    """벤더가 해당 심볼에 대해 사용 가능한 행을 반환하지 않음 (빈 결과 또는 오래된 데이터).

    사용자가 요청한 심볼과 벤더에 실제로 질의한 정규화된(canonical) 심볼을 모두
    담고, 자유 텍스트 ``detail`` 도 함께 전달한다. 덕분에 호출자는 벤더별로 제각각인
    빈 문자열을 데이터 채널에 흘려보내는 대신 명확한 메시지를 만들 수 있다.
    """

    def __init__(self, symbol: str, canonical: str | None = None, detail: str = ""):
        self.symbol = symbol
        self.canonical = canonical or symbol
        self.detail = detail
        msg = f"No market data for {symbol!r}"
        if canonical and canonical != symbol:
            msg += f" (queried as {canonical!r})"
        if detail:
            msg += f": {detail}"
        super().__init__(msg)


class VendorRateLimitError(VendorError):
    """벤더가 요청을 제한(throttle)함; 라우터는 다음 벤더로 넘어간다."""


class VendorNotConfiguredError(VendorError, ValueError):
    """벤더가 선택되었지만 해당 벤더의 API 키/설정이 누락됨.

    ``ValueError`` 이기도 하므로 기존에 ``ValueError`` 를 잡던 호출자는 그대로
    동작하고, 라우팅 계층은 이를 "벤더 사용 불가(vendor unavailable)"로 처리할 수
    있다.
    """
