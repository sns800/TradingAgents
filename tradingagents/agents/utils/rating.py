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

import re

# 표준(canonical) 5단계 척도, 순서 있음(가장 강세 -> 가장 약세).
RATINGS_5_TIER: tuple[str, ...] = (
    "Buy", "Overweight", "Hold", "Underweight", "Sell",
)

_RATING_SET = {r.lower() for r in RATINGS_5_TIER}

# "Rating: X" / "rating - X" / "Rating: **X**" 형태를 매칭 — 마크다운 굵게(**) 래퍼와
# 콜론(:) 또는 하이픈(-) 구분자를 모두 허용한다.
_RATING_LABEL_RE = re.compile(r"rating.*?[:\-][\s*]*(\w+)", re.IGNORECASE)


def parse_rating(text: str, default: str = "Hold") -> str:
    """서술형 텍스트에서 5단계 등급을 휴리스틱하게 추출한다.

    2단계(two-pass) 전략:
    1. 명시적인 "Rating: X" 라벨을 찾는다(마크다운 굵게 표기 허용).
    2. 없으면 텍스트 어디서든 처음 발견되는 5단계 등급 단어로 대체한다.

    첫 글자가 대문자인(Title-cased) 등급 문자열을 반환하고, 등급 단어가
    전혀 없으면 ``default``를 반환한다.
    """
    for line in text.splitlines():
        m = _RATING_LABEL_RE.search(line)
        if m and m.group(1).lower() in _RATING_SET:
            return m.group(1).capitalize()

    for line in text.splitlines():
        for word in line.lower().split():
            clean = word.strip("*:.,")
            if clean in _RATING_SET:
                return clean.capitalize()

    return default
