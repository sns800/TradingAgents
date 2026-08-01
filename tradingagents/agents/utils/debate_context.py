# =============================================================================
# [모듈 개요 - 초보자용]
# 토론자 프롬프트용 토론 이력(history) 압축 헬퍼입니다 (설계분석 중기 로드맵 #3).
#
# 문제: 토론자(강세/약세 리서처, 리스크 3인)의 프롬프트에는 매 발언마다 누적
# history 전문이 통째로 재주입되어 토큰 사용량이 O(라운드²)로 자랐습니다.
# 완화: 프롬프트에 넣는 이력을 "가장 최근 발언은 전문 유지 + 그 이전 발언들은
# 각각 앞 300자로 결정론적 절단"으로 재구성합니다. LLM 요약 호출을 쓰지 않는
# 순수 문자열 절단이므로 추가 비용이 0이고 실행마다 결과가 같습니다.
#
# 중요: 상태(state)에 저장되는 history 원본은 절대 건드리지 않습니다 — 압축은
# 프롬프트를 만들 때만 적용됩니다. 심판(리서치 매니저·포트폴리오 매니저)은
# 전체 history를 계속 받습니다. 판정 근거가 되는 텍스트이기 때문입니다.
# =============================================================================

"""Deterministic condensation of debate history for debater prompts."""

from __future__ import annotations

# 토론 발언은 항상 "<화자 라벨>: <내용>" 형태로 history에 덧붙는다
# (각 토론자 노드가 argument = f"{label}: {content}"로 기록). 이 라벨들이
# 발언 경계를 식별하는 기준이다. 라벨 문자열은 각 토론자 파일과 일치해야 한다.
DEBATE_SPEAKER_LABELS = (
    "Bull Analyst:",
    "Bear Analyst:",
    "Aggressive Analyst:",
    "Conservative Analyst:",
    "Neutral Analyst:",
)

# 과거 발언 하나당 프롬프트에 남기는 최대 문자 수 (화자 라벨 포함).
DEFAULT_SUMMARY_CHARS = 300

# 절단됐음을 토론자에게 알리는 표식. 심판은 전문을 보므로, 토론자가
# "요약본만 보고 상대가 말하지 않은 것을 지어내는" 위험을 줄이기 위해
# 절단 사실을 명시한다.
TRUNCATION_MARKER = " ...[earlier argument truncated]"


def condense_debate_history(
    history: str, max_summary_chars: int = DEFAULT_SUMMARY_CHARS
) -> str:
    """토론 이력을 "직전 발언 전문 + 이전 발언들은 각 300자"로 압축한다.

    발언 경계는 줄 시작의 화자 라벨(DEBATE_SPEAKER_LABELS)로 식별합니다.
    마지막 발언(직전 발언)은 전문을 유지하고, 그 이전 발언들은 각각 앞
    ``max_summary_chars``자만 남깁니다(결정론적 절단 — LLM 호출 없음).
    라벨을 하나도 찾지 못하면(형식 드리프트) 안전하게 원문을 그대로
    반환합니다 — 압축 실패가 정보 손실보다 낫습니다.
    """
    if not history:
        return history

    # 줄 단위로 훑으며 화자 라벨로 시작하는 줄에서 새 발언을 연다.
    # 발언 내용은 여러 줄(문단)일 수 있으므로 다음 라벨 전까지 이어 붙인다.
    statements: list[list[str]] = []
    preamble: list[str] = []  # 첫 라벨 이전 텍스트 (보통 빈 줄뿐)
    for line in history.split("\n"):
        if line.startswith(DEBATE_SPEAKER_LABELS):
            statements.append([line])
        elif statements:
            statements[-1].append(line)
        else:
            preamble.append(line)

    if not statements:
        # 알 수 없는 형식: 절단하지 말고 원문 유지 (안전한 폴백).
        return history

    parts: list[str] = []
    preamble_text = "\n".join(preamble).strip()
    if preamble_text:
        parts.append(preamble_text)

    # 직전 발언을 제외한 과거 발언들: 각각 앞 max_summary_chars자로 절단.
    for statement_lines in statements[:-1]:
        statement = "\n".join(statement_lines)
        if len(statement) > max_summary_chars:
            statement = statement[:max_summary_chars] + TRUNCATION_MARKER
        parts.append(statement)

    # 직전 발언(마지막 발언)은 전문 유지 — 토론자가 반박할 대상이다.
    parts.append("\n".join(statements[-1]))

    return "\n".join(parts)
