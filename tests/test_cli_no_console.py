# 이 파일은 콘솔 버퍼가 없는 터미널(주로 비대화형 Windows 환경)에서 CLI가
# 긴 트레이스백 대신 조치 가능한 한 줄짜리 오류 메시지를 내는지 검증하는
# 테스트 모음입니다.
"""콘솔 버퍼가 없는 터미널에서는 조치 가능한 한 줄 메시지로 실패해야 함 (#1138).

비대화형 Windows 터미널에서는 prompt_toolkit이 첫 프롬프트 이전에
NoConsoleScreenBufferError를 던지는데, CLI가 그 트레이스백을 사용자에게
그대로 노출하면 안 됩니다. 또한 Windows 전용 예외 임포트는 다른 플랫폼에서
아무 영향이 없어야 합니다.
"""
from __future__ import annotations

import sys

from typer.testing import CliRunner

import cli.main as m


def test_no_console_error_tuple_matches_platform():
    """플랫폼에 따라 콘솔 오류 예외 튜플이 올바르게 구성되는지 검증하는 테스트."""
    # Windows가 아니면 win32 모듈은 아예 임포트되지 않으므로(플랫폼을 assert함)
    # 튜플이 비어 있습니다 — 빈 튜플은 `except`가 허용하며 어떤 예외와도 매칭되지
    # 않습니다. Windows에서는 실제 예외 타입이 들어가며, prompt_toolkit이 깨져
    # 있다면 핸들러가 조용히 비활성화되는 대신 임포트 시점에 오류가 납니다.
    assert isinstance(m._NO_CONSOLE_ERRORS, tuple)
    assert all(issubclass(e, BaseException) for e in m._NO_CONSOLE_ERRORS)
    if sys.platform == "win32":
        assert m._NO_CONSOLE_ERRORS, "Windows must resolve the console error type"
    else:
        assert m._NO_CONSOLE_ERRORS == ()


def test_missing_console_prints_actionable_message(monkeypatch):
    """콘솔이 없을 때 조치 가능한 안내 메시지를 출력하는지 검증하는 테스트."""
    class _NoConsole(Exception):
        pass

    # 대역(stand-in) 예외를 등록해 어느 플랫폼에서든 Windows 오류 상황을 흉내 냅니다.
    monkeypatch.setattr(m, "_NO_CONSOLE_ERRORS", (_NoConsole,))

    def _boom(*a, **k):
        raise _NoConsole("No Windows console found. Are you running cmd.exe?")

    monkeypatch.setattr(m, "run_analysis", _boom)

    result = CliRunner().invoke(m.app, [])
    assert result.exit_code == 1
    assert "no Windows console available" in result.output
    # prompt_toolkit의 원본 트레이스백이 사용자에게 도달하면 안 됩니다.
    assert "Traceback" not in result.output


def test_unrelated_errors_still_propagate(monkeypatch):
    """콘솔 오류가 아닌 예외는 그대로 전파되는지 검증하는 테스트."""
    # 핸들러는 좁게 유지되어야 합니다: 콘솔 오류만 친절한 메시지로 변환합니다.
    monkeypatch.setattr(m, "_NO_CONSOLE_ERRORS", (RuntimeError,))

    def _boom(*a, **k):
        raise ValueError("unrelated")

    monkeypatch.setattr(m, "run_analysis", _boom)
    result = CliRunner().invoke(m.app, [])
    assert isinstance(result.exception, ValueError)
