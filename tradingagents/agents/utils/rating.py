# =============================================================================
# [모듈 개요 — 초보자용]
# 이 파일은 TradingAgents 시스템 전체에서 공유하는 5단계 투자 등급(rating) 체계와,
# LLM이 생성한 자유 서술 텍스트에서 등급을 뽑아내는 결정론적(deterministic) 파서를
# 정의합니다. 리서치 매니저, 포트폴리오 매니저, 시그널 처리기, 메모리 로그가
# 모두 같은 등급 어휘를 사용하므로 여기 한곳에 모아 불일치를 방지합니다.
# =============================================================================

"""공유 5단계 등급 어휘와 결정론적 휴리스틱(heuristic) 파서.

동일한 5단계 척도(Buy, Overweight, Hold, Underweight, Sell)를 다음이 사용한다:
- 리서치 매니저(Research Manager) — 투자 계획 추천
- 포트폴리오 매니저(Portfolio Manager) — 최종 포지션 결정
- 시그널 처리기(signal processor) — 하위 소비자용으로 추출되는 등급
- 메모리 로그(memory log) — 각 결정 항목과 함께 저장되는 등급 태그

여기 한곳에 집중시켜 각 호출 지점 간의 어휘 불일치(drift)를 방지한다.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# 표준(canonical) 5단계 척도, 순서 있음(가장 강세 -> 가장 약세).
RATINGS_5_TIER: tuple[str, ...] = (
    "Buy", "Overweight", "Hold", "Underweight", "Sell",
)

_RATING_SET = {r.lower() for r in RATINGS_5_TIER}

# 한국어 등급 어휘 -> 표준 영어 등급 매핑.
# 이 포크는 기본 출력 언어가 한국어라, 구조화 출력이 실패해 자유 텍스트
# 결정문이 한국어로 생성되면 영어 어휘만 아는 파서가 무조건 기본값(Hold)을
# 반환하는 치명적 결합이 있었다. 한국어 등급도 인식해 이를 차단한다.
_KO_RATING_MAP = {
    "매수": "Buy",
    "비중확대": "Overweight",
    "보유": "Hold",
    "비중축소": "Underweight",
    "매도": "Sell",
}

# "Rating: X" / "rating - X" / "Rating: **X**" / "등급: X" 형태를 매칭 —
# 마크다운 굵게(**) 래퍼와 콜론(:) 또는 하이픈(-) 구분자를 모두 허용한다.
# (\w는 유니코드 모드라 한글 등급 값도 캡처된다)
_RATING_LABEL_RE = re.compile(r"(?:rating|등급).*?[:\-][\s*]*(\w+)", re.IGNORECASE)

_STRIP_CHARS = "*:.,()[]\"'"


def _normalize(word: str) -> str | None:
    """토큰 하나를 표준 등급으로 변환한다. 등급 어휘가 아니면 None."""
    clean = word.strip(_STRIP_CHARS).lower()
    if clean in _RATING_SET:
        return clean.capitalize()
    compact = clean.replace(" ", "")
    if compact in _KO_RATING_MAP:
        return _KO_RATING_MAP[compact]
    return None


def parse_rating(text: str, default: str = "Hold", context: str = "") -> str:
    """서술형 텍스트에서 5단계 등급을 휴리스틱하게 추출한다.

    2단계(two-pass) 전략:
    1. 명시적인 "Rating: X" / "등급: X" 라벨을 찾는다(마크다운 굵게 허용).
    2. 없으면 텍스트 어디서든 처음 발견되는 5단계 등급 단어(영어/한국어)로
       대체한다. "비중 확대"처럼 띄어 쓴 형태도 라인 단위로 인식한다.

    표준 영어 등급 문자열을 반환하고, 등급 어휘가 전혀 없으면 ``default``를
    반환하되 경고를 로깅한다 — 조용한 Hold 고착은 시그널을 무의미하게
    만들기 때문에 반드시 흔적을 남긴다. ``context``는 경고에 표시할
    호출처 설명이다.
    """
    for line in text.splitlines():
        m = _RATING_LABEL_RE.search(line)
        if m:
            rating = _normalize(m.group(1))
            if rating:
                return rating

    for line in text.splitlines():
        for word in line.lower().split():
            rating = _normalize(word)
            if rating:
                return rating
        # "비중 확대/축소"처럼 띄어 쓴 두 단어 형태는 토큰 검사에 걸리지
        # 않으므로 공백을 제거한 라인에서 한 번 더 확인한다.
        compact = line.replace(" ", "")
        for ko, canonical in _KO_RATING_MAP.items():
            if len(ko) > 2 and ko in compact:
                return canonical

    logger.warning(
        "parse_rating: no rating vocabulary found%s; defaulting to %r. "
        "The decision text may lack an explicit rating or use an "
        "unsupported language.",
        f" ({context})" if context else "", default,
    )
    return default
