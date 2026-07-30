# =============================================================================
# [모듈 개요 - 초보자용]
# 이 파일은 TradingAgents CLI에서 사용하는 선택지들을 열거형(Enum)으로 정의합니다.
# 사용자가 CLI에서 고를 수 있는 분석가 종류(AnalystType)와
# 자산 종류(AssetType: 주식/암호화폐)를 문자열 상수로 안전하게 관리합니다.
# str을 함께 상속하므로 각 멤버는 일반 문자열처럼 비교·저장할 수 있습니다.
# =============================================================================

from enum import Enum


class AnalystType(str, Enum):
    MARKET = "market"
    # 저장된 설정(saved-config) 및 문자열 키로 호출하는 기존 코드와의
    # 하위 호환(back-compat)을 위해 내부 값(wire value)은 "social"로 유지한다.
    # 사용자에게 보이는 이름(label)은 "Sentiment Analyst"이다.
    SOCIAL = "social"
    NEWS = "news"
    FUNDAMENTALS = "fundamentals"


class AssetType(str, Enum):
    STOCK = "stock"
    CRYPTO = "crypto"
