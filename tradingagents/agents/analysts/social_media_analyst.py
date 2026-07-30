# ============================================================================
# 소셜 미디어 분석가(Social Media Analyst) — 하위 호환용 모듈
#
# 이 모듈은 실제 로직이 없는 하위 호환성(backwards-compatibility) 심(shim)입니다.
# 해당 에이전트는 감성 분석가(sentiment_analyst)로 이름이 바뀌고 재설계되었으며,
# 전체 파이프라인(분석가 → 리서처 토론 → 트레이더 → 리스크 토론 → 포트폴리오 매니저)에서
# "분석가" 팀의 시장 심리(sentiment) 담당 역할은 sentiment_analyst 모듈이 수행합니다.
# 여기서는 옛 임포트 경로가 깨지지 않도록 재수출(re-export)만 합니다.
# ============================================================================

"""이름이 변경된 모듈을 위한 하위 호환성(backwards-compatibility) 심(shim).

이 에이전트는 이제 ``sentiment_analyst`` 이며, Yahoo Finance 뉴스,
StockTwits 캐시태그(cashtag) 스트림, Reddit 게시글을 하나의 감성 보고서로
통합합니다. 앞으로는 ``tradingagents.agents.analysts.sentiment_analyst`` 에서
임포트하세요. 이 모듈은 향후 릴리스에서 제거될 예정입니다.

참고: https://github.com/TauricResearch/TradingAgents/issues/557
"""

import warnings as _warnings

# 새 모듈에서 함수들을 재수출(re-export)하여 기존 임포트 경로를 유지합니다.
from tradingagents.agents.analysts.sentiment_analyst import (  # noqa: F401
    create_sentiment_analyst,
    create_social_media_analyst,
)

# 이 모듈을 임포트하면 사용 중단(deprecation) 경고를 발생시킵니다.
_warnings.warn(
    "tradingagents.agents.analysts.social_media_analyst is deprecated. "
    "Import from tradingagents.agents.analysts.sentiment_analyst instead.",
    DeprecationWarning,
    stacklevel=2,
)
