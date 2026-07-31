# =============================================================================
# [모듈 개요 - 초보자용]
# 이 파일은 TradingAgents CLI가 시작될 때 화면에 보여줄 공지사항(announcements)을
# 원격 서버에서 가져와 표시하는 기능을 담당합니다.
# 네트워크 오류가 나더라도 CLI 실행이 막히지 않도록, 실패 시에는
# 미리 정해둔 대체 문구(fallback)를 보여줍니다. cli/main.py에서 호출됩니다.
# =============================================================================

import getpass

import requests
from rich.console import Console
from rich.panel import Panel

from cli.config import CLI_CONFIG


def fetch_announcements(url: str = None, timeout: float = None) -> dict:
    """엔드포인트(endpoint)에서 공지사항을 가져온다. 공지 목록과 설정이 담긴 dict를 반환한다."""
    # 인자를 넘기지 않으면 CLI_CONFIG에 정의된 기본 URL/타임아웃(timeout)을 사용
    endpoint = url or CLI_CONFIG["announcements_url"]
    timeout = timeout or CLI_CONFIG["announcements_timeout"]
    fallback = CLI_CONFIG["announcements_fallback"]

    try:
        response = requests.get(endpoint, timeout=timeout)
        response.raise_for_status()
        data = response.json()
        return {
            "announcements": data.get("announcements", [fallback]),
            "require_attention": data.get("require_attention", False),
        }
    except Exception:
        # 네트워크 오류, 타임아웃, JSON 파싱 실패 등 어떤 예외가 나도
        # CLI 실행을 막지 않기 위해 대체 문구(fallback)만 반환한다.
        return {
            "announcements": [fallback],
            "require_attention": False,
        }


def display_announcements(console: Console, data: dict) -> None:
    """공지사항 패널(panel)을 출력한다. require_attention이 True면 Enter 입력을 기다린다."""
    announcements = data.get("announcements", [])
    require_attention = data.get("require_attention", False)

    if not announcements:
        return

    content = "\n".join(announcements)

    panel = Panel(
        content,
        border_style="cyan",
        padding=(1, 2),
        title="공지사항",
    )
    console.print(panel)

    if require_attention:
        # getpass를 쓰면 입력한 내용이 화면에 표시되지 않아
        # "Enter를 눌러 계속" 용도로 깔끔하게 대기할 수 있다.
        getpass.getpass("계속하려면 Enter를 누르세요...")
    else:
        console.print()
