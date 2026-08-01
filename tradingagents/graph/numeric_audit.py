# TradingAgents/graph/numeric_audit.py
#
# [모듈 개요 - 초보자용]
# 이 파일은 포트폴리오 매니저(PM)의 최종 결정문(final_trade_decision)에 인용된
# 달러 가격($123.45, $1,234 같은 표기)을, 시장 분석가가 보존해 둔 검증 스냅샷
# (verified_snapshot 상태 필드)의 수치 집합과 결정론적으로(LLM 없이) 대조하는
# 사후 수치 감사(numeric audit)를 구현합니다(설계분석 중기 로드맵 #5).
#
# 배경: Msg Clear가 원본 도구 데이터를 파기하기 때문에, 기존에는 하류 에이전트가
# 인용한 수치가 실제 데이터와 맞는지 잡을 기준점이 전혀 없었습니다. 스냅샷이
# 상태 필드로 보존되면서 이 감사가 가능해졌습니다.
#
# 설계 원칙:
# - 결정 자체는 절대 바꾸지 않습니다. 스냅샷에 없는 가격 인용을 발견하면
#   결정문 끝에 경고 블록만 덧붙입니다 (등급 파싱 parse_rating에 영향 없음).
# - 오탐(false positive)을 억제합니다: 달러 기호가 붙은 가격 패턴만 검사하고,
#   "$5 billion" 같은 규모 표현은 건너뛰며, 목표가/손절가처럼 계산된 값일 수
#   있으므로 "스냅샷에서 찾지 못했다"는 나열식 경고만 하고 강한 주장(오류
#   단정)은 하지 않습니다.
# - 스냅샷이 비어 있으면(스냅샷 도구 미호출, NO_DATA, 구형 체크포인트)
#   감사를 통째로 생략합니다.
#
# trading_graph._run_graph가 PM 완료 후 이 모듈의 audit_final_decision을
# 호출합니다 — 그래프 노드로 추가하는 대신 함수 호출로 적용해 그래프 모양
# (체크포인트 시그니처)을 바꾸지 않는 최소 침습 방식입니다.

"""PM 결정문의 달러 가격 인용을 검증 스냅샷과 대조하는 결정론적 사후 감사."""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# 경고 블록의 식별 접두사. 이미 감사된 결정문의 재감사(중복 경고)를 막고,
# 테스트가 경고 블록의 존재를 확인하는 기준으로도 쓰인다.
AUDIT_WARNING_PREFIX = "⚠️ Numeric audit"

# 달러 가격 패턴: $123.45 / $1,234 / $1,234.56 / $12 (천 단위 콤마와 소수점
# 모두 선택적). 달러 기호 없는 맨 숫자는 날짜·백분율·수량과 구분할 수 없어
# 오탐 폭탄이 되므로 의도적으로 검사하지 않는다.
_DOLLAR_PRICE_RE = re.compile(r"\$\s*(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?")

# 가격이 아닌 규모 표현("$5 billion", "$3T", "$20bn" 등)의 접미사.
# 이런 금액은 시가총액·매출 같은 다른 축의 수치라 스냅샷과 대조할 수 없다.
_SCALE_SUFFIX_RE = re.compile(
    r"^\s*(?:million|billion|trillion|thousand|bn\b|mn\b|tn\b|[kmbt]\b)",
    re.IGNORECASE,
)

# 스냅샷 안의 숫자(마크다운 표의 값들). 콤마가 섞여 있어도 허용한다.
_SNAPSHOT_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def extract_cited_prices(text: str) -> list[tuple[str, float]]:
    """결정문에서 달러 가격 인용을 (표시 문자열, 숫자 값) 쌍으로 추출한다.

    표시 문자열은 "$1,234.56"처럼 정규화된 형태(달러 기호와 공백 제거)이고,
    같은 표시 문자열은 한 번만 반환한다(등장 순서 유지). "$5 billion" 같은
    규모 표현은 가격이 아니므로 건너뛴다.
    """
    results: list[tuple[str, float]] = []
    seen: set[str] = set()
    for match in _DOLLAR_PRICE_RE.finditer(text):
        if _SCALE_SUFFIX_RE.match(text[match.end():]):
            continue  # 규모 표현($5 billion 등)은 가격 인용이 아니다
        int_part, dec_part = match.group(1), match.group(2) or ""
        display = f"${int_part}{dec_part}"
        if display in seen:
            continue
        seen.add(display)
        results.append((display, float(int_part.replace(",", "") + dec_part)))
    return results


def extract_snapshot_numbers(snapshot: str) -> list[float]:
    """스냅샷 본문에서 대조 기준이 될 모든 숫자를 추출한다.

    스냅샷은 OHLCV·지표·최근 종가를 담은 마크다운 표라 값이 맨 숫자로
    적혀 있다. 날짜 조각(2026, 01 등)도 함께 추출되지만, 기준 집합이
    넓어지는 방향(경고가 줄어드는 방향)의 오차라 안전하다 — 이 감사의
    우선순위는 오탐 억제이기 때문이다.
    """
    numbers: list[float] = []
    for token in _SNAPSHOT_NUMBER_RE.findall(snapshot):
        try:
            numbers.append(float(token.replace(",", "")))
        except ValueError:  # pragma: no cover - 정규식상 도달 불가, 방어적 처리
            continue
    return numbers


def _is_supported(value: float, snapshot_values: list[float], tolerance: float) -> bool:
    """인용 값이 스냅샷 수치와 일치(정확히 또는 허용 오차 이내)하는지 판정한다."""
    for candidate in snapshot_values:
        if value == candidate:
            return True
        if candidate != 0 and abs(value - candidate) <= tolerance * abs(candidate):
            return True
    return False


def audit_final_decision(
    decision: str, snapshot: str, tolerance: float = 0.01
) -> str:
    """결정문의 달러 가격 인용을 스냅샷과 대조하고, 미발견 시 경고 블록을 덧붙인다.

    반환값은 항상 결정문 텍스트다: 문제가 없거나 감사가 불가능하면(빈
    스냅샷, 가격 인용 없음, 이미 감사됨) 원문 그대로, 스냅샷에서 찾지
    못한 가격이 있으면 끝에 경고 블록을 붙인 사본을 반환한다. 결정의
    등급(Rating)이나 본문은 절대 수정하지 않으므로 parse_rating 결과가
    바뀌지 않는다. 경고 문구는 하류 소비자(보고서·메모리)가 그대로
    저장하므로 영어를 유지한다.
    """
    if not isinstance(decision, str) or not decision.strip():
        return decision
    if not isinstance(snapshot, str) or not snapshot.strip():
        return decision  # 기준점이 없으면 감사를 생략한다
    if AUDIT_WARNING_PREFIX in decision:
        return decision  # 이미 감사된 결정문(재실행·재개)에 중복 경고 방지

    cited = extract_cited_prices(decision)
    if not cited:
        return decision

    snapshot_values = extract_snapshot_numbers(snapshot)
    if not snapshot_values:
        return decision

    unmatched = [
        display
        for display, value in cited
        if not _is_supported(value, snapshot_values, tolerance)
    ]
    if not unmatched:
        return decision

    logger.warning(
        "Numeric audit: %d cited price(s) not found in the verified market "
        "snapshot (tolerance ±%.2f%%): %s",
        len(unmatched), tolerance * 100, ", ".join(unmatched),
    )

    # [한국어 요약] 경고 블록: "다음 인용 가격은 검증 스냅샷에서 (±허용 오차
    # 이내로) 찾을 수 없었다. 계산된 목표가/손절가일 수도 있으니 오류로
    # 단정하지는 않되, 신뢰하기 전에 스냅샷과 대조해 확인하라."
    warning = (
        f"\n\n---\n\n{AUDIT_WARNING_PREFIX}: the following cited prices were "
        f"not found in the verified market snapshot (within ±{tolerance:.0%}): "
        + ", ".join(unmatched)
        + ". They may be derived levels (e.g., computed price targets or "
        "stop-losses) rather than data errors, but verify them against the "
        "verified snapshot before relying on them."
    )
    return decision + warning
