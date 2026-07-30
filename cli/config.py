# =============================================================================
# [모듈 개요 - 초보자용]
# 이 파일은 TradingAgents CLI 전용 설정값을 한곳에 모아둔 설정(config) 모듈입니다.
# 공지사항(announcements) 조회 URL, 타임아웃(timeout), 실패 시 대체 문구(fallback)
# 등을 정의하며, cli/announcements.py 등에서 읽어 사용합니다.
# 에이전트 자체 설정은 tradingagents/default_config.py가 따로 담당합니다.
# =============================================================================

CLI_CONFIG = {
    # 공지사항(Announcements) 관련 설정
    "announcements_url": "https://api.tauric.ai/v1/announcements",
    "announcements_timeout": 1.0,
    "announcements_fallback": "[cyan]For more information, please visit[/cyan] [link=https://github.com/TauricResearch]https://github.com/TauricResearch[/link]",
}
