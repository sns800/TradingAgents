# ============================================================
# [모듈 개요] TradingAgents CLI 진입점(entry point)
# TradingAgents는 여러 LLM 에이전트(애널리스트, 리서처, 트레이더,
# 리스크 관리자 등)가 협업해 주식 매매 결정을 내리는 멀티 에이전트
# 트레이딩 프레임워크입니다. 이 파일은 사용자에게 대화형으로 설정을
# 입력받고(종목, 날짜, LLM 공급자 등) 분석 그래프를 실행하면서,
# Rich 라이브러리로 터미널에 실시간 진행 상황을 그려 주는 CLI입니다.
# ============================================================
import datetime
import os
import sys
import time
from collections import deque
from functools import wraps
from pathlib import Path

import typer
from rich import box
from rich.align import Align
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from cli.announcements import display_announcements, fetch_announcements
from cli.stats_handler import StatsCallbackHandler
from cli.utils import (
    ask_anthropic_effort,
    ask_gemini_thinking_config,
    ask_glm_region,
    ask_minimax_region,
    ask_openai_reasoning_effort,
    ask_output_language,
    ask_qwen_region,
    confirm_ollama_endpoint,
    detect_asset_type,
    ensure_api_key,
    get_ticker,
    prompt_openai_compatible_url,
    resolve_backend_url,
    select_analysts,
    select_deep_thinking_agent,
    select_llm_provider,
    select_research_depth,
    select_shallow_thinking_agent,
)
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.analyst_execution import (
    AnalystWallTimeTracker,
    build_analyst_execution_plan,
    get_initial_analyst_node,
    sync_analyst_tracker_from_chunk,
)
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.reporting import write_report_tree

console = Console()

# prompt_toolkit의 win32 출력 모듈은 Windows에서만 임포트할 수 있다
# (임포트 시점에 플랫폼을 검사(assert)한다). 따라서 실패를 잡아내는 대신
# 플랫폼으로 분기한다 — 이렇게 하면 Windows에서 prompt_toolkit이 정말로
# 고장 난 경우에도 아래 핸들러가 조용히 비활성화되지 않고 오류가 드러난다.
# Windows가 아니면 빈 튜플로 남는데, `except`는 빈 튜플을 받으면 아무것도
# 매칭하지 않는다 (#1138).
if sys.platform == "win32":  # pragma: no cover - platform dependent
    from prompt_toolkit.output.win32 import NoConsoleScreenBufferError

    _NO_CONSOLE_ERRORS: tuple[type[BaseException], ...] = (NoConsoleScreenBufferError,)
else:
    _NO_CONSOLE_ERRORS = ()

app = typer.Typer(
    name="TradingAgents",
    help="TradingAgents CLI: 멀티 에이전트 LLM 금융 트레이딩 프레임워크",
    add_completion=True,  # 셸 자동완성(shell completion) 활성화
)


# 최근 메시지를 최대 길이가 제한된 덱(deque)에 저장하는 버퍼 클래스.
# 화면에 표시할 메시지/도구 호출/에이전트 상태/보고서 섹션을 한곳에 모아 관리한다.
class MessageBuffer:
    # 항상 실행되는 고정 팀 (사용자가 선택할 수 없음)
    FIXED_AGENTS = {
        "Research Team": ["Bull Researcher", "Bear Researcher", "Research Manager"],
        "Trading Team": ["Trader"],
        "Risk Management": ["Aggressive Analyst", "Neutral Analyst", "Conservative Analyst"],
        "Portfolio Management": ["Portfolio Manager"],
    }

    # 애널리스트(analyst) 키 → 표시 이름 매핑
    ANALYST_MAPPING = {
        "market": "Market Analyst",
        "social": "Sentiment Analyst",
        "news": "News Analyst",
        "fundamentals": "Fundamentals Analyst",
    }

    # 보고서 섹션 매핑: 섹션 -> (필터링용 analyst_key, 마무리 담당 에이전트)
    # analyst_key: 이 섹션의 포함 여부를 결정하는 애널리스트 선택 키 (None = 항상 포함)
    # finalizing_agent: 이 에이전트가 "completed" 상태여야 해당 보고서가 완료로 집계됨
    REPORT_SECTIONS = {
        "market_report": ("market", "Market Analyst"),
        "sentiment_report": ("social", "Sentiment Analyst"),
        "news_report": ("news", "News Analyst"),
        "fundamentals_report": ("fundamentals", "Fundamentals Analyst"),
        "investment_plan": (None, "Research Manager"),
        "trader_investment_plan": (None, "Trader"),
        "final_trade_decision": (None, "Portfolio Manager"),
    }

    def __init__(self, max_length=100):
        self.messages = deque(maxlen=max_length)
        self.tool_calls = deque(maxlen=max_length)
        self.current_report = None
        self.final_report = None  # 완성된 최종 보고서 전체를 저장
        self.agent_status = {}
        self.current_agent = None
        self.report_sections = {}
        self.selected_analysts = []
        self._processed_message_ids = set()

    def init_for_analysis(self, selected_analysts):
        """선택된 애널리스트를 기준으로 에이전트 상태와 보고서 섹션을 초기화한다.

        Args:
            selected_analysts: 애널리스트 유형 문자열 리스트 (예: ["market", "news"])
        """
        self.selected_analysts = [a.lower() for a in selected_analysts]

        # agent_status를 동적으로 구성
        self.agent_status = {}

        # 선택된 애널리스트 추가
        for analyst_key in self.selected_analysts:
            if analyst_key in self.ANALYST_MAPPING:
                self.agent_status[self.ANALYST_MAPPING[analyst_key]] = "pending"

        # 고정 팀 추가
        for team_agents in self.FIXED_AGENTS.values():
            for agent in team_agents:
                self.agent_status[agent] = "pending"

        # report_sections를 동적으로 구성
        self.report_sections = {}
        for section, (analyst_key, _) in self.REPORT_SECTIONS.items():
            if analyst_key is None or analyst_key in self.selected_analysts:
                self.report_sections[section] = None

        # 나머지 상태 초기화
        self.current_report = None
        self.final_report = None
        self.current_agent = None
        self.messages.clear()
        self.tool_calls.clear()
        self._processed_message_ids.clear()

    def get_completed_reports_count(self):
        """확정된 보고서(마무리 담당 에이전트가 완료된 보고서)의 개수를 센다.

        보고서는 다음 두 조건을 모두 만족할 때 완료로 간주한다:
        1. 보고서 섹션에 내용이 있고(None이 아님),
        2. 해당 보고서를 마무리하는 에이전트의 상태가 "completed"일 것.

        이렇게 하면 중간 업데이트(예: 토론 라운드)가 완료로 잘못 집계되는 것을 막는다.
        """
        count = 0
        for section in self.report_sections:
            if section not in self.REPORT_SECTIONS:
                continue
            _, finalizing_agent = self.REPORT_SECTIONS[section]
            # 내용이 있고 마무리 담당 에이전트까지 끝났을 때만 보고서 완료
            has_content = self.report_sections.get(section) is not None
            agent_done = self.agent_status.get(finalizing_agent) == "completed"
            if has_content and agent_done:
                count += 1
        return count

    def add_message(self, message_type, content):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.messages.append((timestamp, message_type, content))

    def add_tool_call(self, tool_name, args):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.tool_calls.append((timestamp, tool_name, args))

    def update_agent_status(self, agent, status):
        if agent in self.agent_status:
            self.agent_status[agent] = status
            self.current_agent = agent

    def update_report_section(self, section_name, content):
        if section_name in self.report_sections:
            self.report_sections[section_name] = content
            self._update_current_report()

    def _update_current_report(self):
        # 패널 표시용으로는 가장 최근에 갱신된 섹션만 보여준다
        latest_section = None
        latest_content = None

        # 가장 최근에 갱신된 섹션을 찾는다 (순회하며 내용이 있는 마지막 항목이 남음)
        for section, content in self.report_sections.items():
            if content is not None:
                latest_section = section
                latest_content = content

        if latest_section and latest_content:
            # 현재 섹션을 화면 표시용으로 포맷
            section_titles = {
                "market_report": "시장 분석",
                "sentiment_report": "소셜 감성 분석",
                "news_report": "뉴스 분석",
                "fundamentals_report": "기본적 분석",
                "investment_plan": "리서치 팀 결정",
                "trader_investment_plan": "트레이딩 팀 계획",
                "final_trade_decision": "포트폴리오 관리 결정",
            }
            self.current_report = (
                f"### {section_titles[latest_section]}\n{latest_content}"
            )

        # 최종 전체 보고서도 함께 갱신
        self._update_final_report()

    def _update_final_report(self):
        report_parts = []

        # 애널리스트 팀 보고서 - 누락된 섹션 처리를 위해 .get() 사용
        analyst_sections = ["market_report", "sentiment_report", "news_report", "fundamentals_report"]
        if any(self.report_sections.get(section) for section in analyst_sections):
            report_parts.append("## 분석가 팀 보고서")
            if self.report_sections.get("market_report"):
                report_parts.append(
                    f"### 시장 분석\n{self.report_sections['market_report']}"
                )
            if self.report_sections.get("sentiment_report"):
                report_parts.append(
                    f"### 소셜 감성 분석\n{self.report_sections['sentiment_report']}"
                )
            if self.report_sections.get("news_report"):
                report_parts.append(
                    f"### 뉴스 분석\n{self.report_sections['news_report']}"
                )
            if self.report_sections.get("fundamentals_report"):
                report_parts.append(
                    f"### 기본적 분석\n{self.report_sections['fundamentals_report']}"
                )

        # 리서치 팀 보고서
        if self.report_sections.get("investment_plan"):
            report_parts.append("## 리서치 팀 결정")
            report_parts.append(f"{self.report_sections['investment_plan']}")

        # 트레이딩 팀 보고서
        if self.report_sections.get("trader_investment_plan"):
            report_parts.append("## 트레이딩 팀 계획")
            report_parts.append(f"{self.report_sections['trader_investment_plan']}")

        # 포트폴리오 관리 결정
        if self.report_sections.get("final_trade_decision"):
            report_parts.append("## 포트폴리오 관리 결정")
            report_parts.append(f"{self.report_sections['final_trade_decision']}")

        self.final_report = "\n\n".join(report_parts) if report_parts else None


message_buffer = MessageBuffer()


def create_layout():
    # Rich의 Layout으로 터미널 화면을 영역별로 분할한다.
    # 구조: header(상단 3줄) / main(중앙) / footer(하단 3줄)
    #   main은 다시 upper(위)와 analysis(아래, 보고서)로 세로 분할되고,
    #   upper는 progress(왼쪽, 진행 상황)와 messages(오른쪽)로 가로 분할된다.
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="main"),
        Layout(name="footer", size=3),
    )
    layout["main"].split_column(
        Layout(name="upper", ratio=3), Layout(name="analysis", ratio=5)
    )
    layout["upper"].split_row(
        Layout(name="progress", ratio=2), Layout(name="messages", ratio=3)
    )
    return layout


def format_tokens(n):
    """토큰(token) 수를 표시용 문자열로 포맷한다 (1000 이상이면 k 단위)."""
    if n >= 1000:
        return f"{n/1000:.1f}k"
    return str(n)


def update_display(layout, spinner_text=None, stats_handler=None, start_time=None):
    # 매 갱신 주기마다 호출되어 Rich Live 화면의 각 영역
    # (헤더/진행 상황/메시지/분석/푸터)을 최신 상태로 다시 그린다.

    # 헤더: 환영 메시지
    layout["header"].update(
        Panel(
            "[bold green]TradingAgents CLI에 오신 것을 환영합니다[/bold green]\n"
            "[dim]© [Tauric Research](https://github.com/TauricResearch)[/dim]",
            title="TradingAgents에 오신 것을 환영합니다",
            border_style="green",
            padding=(1, 2),
            expand=True,
        )
    )

    # 진행 상황 패널: 에이전트 상태 표시
    progress_table = Table(
        show_header=True,
        header_style="bold magenta",
        show_footer=False,
        box=box.SIMPLE_HEAD,  # 가로줄이 있는 단순 헤더 스타일 사용
        title=None,  # 중복되는 Progress 제목 제거
        padding=(0, 2),  # 가로 여백 추가
        expand=True,  # 테이블이 가용 공간을 채우도록 확장
    )
    progress_table.add_column("팀", style="cyan", justify="center", width=20)
    progress_table.add_column("에이전트", style="green", justify="center", width=20)
    progress_table.add_column("상태", style="yellow", justify="center", width=20)

    # 에이전트를 팀별로 묶는다 - agent_status에 있는 에이전트만 포함하도록 필터링
    all_teams = {
        "Analyst Team": [
            "Market Analyst",
            "Sentiment Analyst",
            "News Analyst",
            "Fundamentals Analyst",
        ],
        "Research Team": ["Bull Researcher", "Bear Researcher", "Research Manager"],
        "Trading Team": ["Trader"],
        "Risk Management": ["Aggressive Analyst", "Neutral Analyst", "Conservative Analyst"],
        "Portfolio Management": ["Portfolio Manager"],
    }

    # agent_status에 존재하는 에이전트만 남기도록 팀 필터링
    teams = {}
    for team, agents in all_teams.items():
        active_agents = [a for a in agents if a in message_buffer.agent_status]
        if active_agents:
            teams[team] = active_agents

    for team, agents in teams.items():
        # 첫 번째 에이전트는 팀 이름과 함께 추가
        first_agent = agents[0]
        status = message_buffer.agent_status.get(first_agent, "pending")
        if status == "in_progress":
            spinner = Spinner(
                "dots", text="[blue]in_progress[/blue]", style="bold cyan"
            )
            status_cell = spinner
        else:
            status_color = {
                "pending": "yellow",
                "completed": "green",
                "error": "red",
            }.get(status, "white")
            status_cell = f"[{status_color}]{status}[/{status_color}]"
        progress_table.add_row(team, first_agent, status_cell)

        # 팀의 나머지 에이전트 추가 (팀 이름 칸은 비움)
        for agent in agents[1:]:
            status = message_buffer.agent_status.get(agent, "pending")
            if status == "in_progress":
                spinner = Spinner(
                    "dots", text="[blue]in_progress[/blue]", style="bold cyan"
                )
                status_cell = spinner
            else:
                status_color = {
                    "pending": "yellow",
                    "completed": "green",
                    "error": "red",
                }.get(status, "white")
                status_cell = f"[{status_color}]{status}[/{status_color}]"
            progress_table.add_row("", agent, status_cell)

        # 각 팀 뒤에 가로 구분선 추가
        progress_table.add_row("─" * 20, "─" * 20, "─" * 20, style="dim")

    layout["progress"].update(
        Panel(progress_table, title="진행 상황", border_style="cyan", padding=(1, 2))
    )

    # 메시지 패널: 최근 메시지와 도구 호출(tool call) 표시
    messages_table = Table(
        show_header=True,
        header_style="bold magenta",
        show_footer=False,
        expand=True,  # 테이블이 가용 공간을 채우도록 확장
        box=box.MINIMAL,  # 가벼운 느낌을 위한 최소 테두리 스타일
        show_lines=True,  # 가로줄 유지
        padding=(0, 1),  # 열 사이 여백 추가
    )
    messages_table.add_column("시간", style="cyan", width=8, justify="center")
    messages_table.add_column("유형", style="green", width=10, justify="center")
    messages_table.add_column(
        "내용", style="white", no_wrap=False, ratio=1
    )  # 내용 열이 확장되도록 설정

    # 도구 호출과 메시지를 하나로 합친다
    all_messages = []

    # 도구 호출 추가
    for timestamp, tool_name, args in message_buffer.tool_calls:
        formatted_args = format_tool_args(args)
        all_messages.append((timestamp, "Tool", f"{tool_name}: {formatted_args}"))

    # 일반 메시지 추가
    for timestamp, msg_type, content in message_buffer.messages:
        content_str = str(content) if content else ""
        if len(content_str) > 200:
            content_str = content_str[:197] + "..."
        all_messages.append((timestamp, msg_type, content_str))

    # 타임스탬프 내림차순 정렬 (최신이 먼저)
    all_messages.sort(key=lambda x: x[0], reverse=True)

    # 가용 공간에 맞춰 표시할 메시지 개수 결정
    max_messages = 12

    # 앞에서 N개(최신 메시지)만 가져온다
    recent_messages = all_messages[:max_messages]

    # 메시지를 테이블에 추가 (이미 최신순으로 정렬됨)
    for timestamp, msg_type, content in recent_messages:
        # 자동 줄바꿈(word wrapping)이 되도록 내용 포맷
        wrapped_content = Text(content, overflow="fold")
        messages_table.add_row(timestamp, msg_type, wrapped_content)

    layout["messages"].update(
        Panel(
            messages_table,
            title="메시지 및 도구",
            border_style="blue",
            padding=(1, 2),
        )
    )

    # 분석 패널: 현재 보고서 표시
    if message_buffer.current_report:
        layout["analysis"].update(
            Panel(
                Markdown(message_buffer.current_report),
                title="현재 보고서",
                border_style="green",
                padding=(1, 2),
            )
        )
    else:
        layout["analysis"].update(
            Panel(
                "[italic]분석 보고서를 기다리는 중...[/italic]",
                title="현재 보고서",
                border_style="green",
                padding=(1, 2),
            )
        )

    # 푸터: 통계 정보
    # 에이전트 진행률 - agent_status 딕셔너리에서 계산
    agents_completed = sum(
        1 for status in message_buffer.agent_status.values() if status == "completed"
    )
    agents_total = len(message_buffer.agent_status)

    # 보고서 진행률 - 내용 존재 여부가 아니라 에이전트 완료 기준
    reports_completed = message_buffer.get_completed_reports_count()
    reports_total = len(message_buffer.report_sections)

    # 통계 문자열 조각 구성
    stats_parts = [f"에이전트: {agents_completed}/{agents_total}"]

    # 콜백 핸들러(callback handler)에서 가져온 LLM/도구 통계
    if stats_handler:
        stats = stats_handler.get_stats()
        stats_parts.append(f"LLM 호출: {stats['llm_calls']}")
        stats_parts.append(f"도구 호출: {stats['tool_calls']}")

        # 토큰 표시 (값이 없으면 -- 로 대체)
        if stats["tokens_in"] > 0 or stats["tokens_out"] > 0:
            tokens_str = f"\ud1a0\ud070: {format_tokens(stats['tokens_in'])}\u2191 {format_tokens(stats['tokens_out'])}\u2193"
        else:
            tokens_str = "\ud1a0\ud070: --"
        stats_parts.append(tokens_str)

    stats_parts.append(f"\ubcf4\uace0\uc11c: {reports_completed}/{reports_total}")

    # 경과 시간
    if start_time:
        elapsed = time.time() - start_time
        elapsed_str = f"\u23f1 {int(elapsed // 60):02d}:{int(elapsed % 60):02d}"
        stats_parts.append(elapsed_str)

    stats_table = Table(show_header=False, box=None, padding=(0, 2), expand=True)
    stats_table.add_column("Stats", justify="center")
    stats_table.add_row(" | ".join(stats_parts))

    layout["footer"].update(Panel(stats_table, border_style="grey50"))


def get_user_selections():
    """분석 화면을 시작하기 전에 사용자 선택 사항을 모두 입력받는다."""
    # 아스키 아트(ASCII art) 환영 메시지 표시
    with open(Path(__file__).parent / "static" / "welcome.txt", encoding="utf-8") as f:
        welcome_ascii = f.read()

    # 환영 박스 내용 구성
    welcome_content = f"{welcome_ascii}\n"
    welcome_content += "[bold green]TradingAgents: 멀티 에이전트 LLM 금융 트레이딩 프레임워크 - CLI[/bold green]\n\n"
    welcome_content += "[bold]워크플로 단계:[/bold]\n"
    welcome_content += "I. Analyst Team → II. Research Team → III. Trader → IV. Risk Management → V. Portfolio Management\n\n"
    welcome_content += (
        "[dim]제작: [Tauric Research](https://github.com/TauricResearch)[/dim]"
    )

    # 환영 박스를 만들어 가운데 정렬
    welcome_box = Panel(
        welcome_content,
        border_style="green",
        padding=(1, 2),
        title="TradingAgents에 오신 것을 환영합니다",
        subtitle="멀티 에이전트 LLM 금융 트레이딩 프레임워크",
    )
    console.print(Align.center(welcome_box))
    console.print()
    console.print()  # 공지 앞에 세로 여백 추가

    # 공지사항을 가져와 표시 (실패 시 조용히 넘어감)
    announcements = fetch_announcements()
    display_announcements(console, announcements)

    # 각 단계별 질문을 박스로 감싸 표시하는 헬퍼(helper)
    def create_question_box(title, prompt, default=None):
        box_content = f"[bold]{title}[/bold]\n"
        box_content += f"[dim]{prompt}[/dim]"
        if default:
            box_content += f"\n[dim]기본값: {default}[/dim]"
        return Panel(box_content, border_style="blue", padding=(1, 2))

    def thinking_value_or_prompt(env_var, config_key, label, box_title, box_body, prompt_fn):
        """환경변수로 설정된 추론(reasoning)/사고(thinking) 값을 반환하거나, 없으면 물어본다.

        ``env_var``가 설정돼 있으면 대화형 선택을 건너뛰고 환경변수 오버레이가
        DEFAULT_CONFIG에 넣어 둔 값을 사용한다 — 다른 선택 단계에 적용된
        환경변수 우선(env-precedence) 규칙과 동일하다.
        """
        if os.environ.get(env_var):
            value = DEFAULT_CONFIG[config_key]
            console.print(f"[green]✓ 환경변수에서 {label} 설정됨:[/green] {value}")
            return value
        console.print(create_question_box(box_title, box_body))
        return prompt_fn()

    # 1단계: 종목 코드(ticker symbol)
    console.print(
        create_question_box(
            "1단계: 종목 코드(Ticker)",
            "종목 코드를 입력하세요. 필요하면 거래소 접미사를 붙입니다 (예: SPY, 0700.HK, BTC-USD)",
            "SPY",
        )
    )
    selected_ticker = get_ticker()
    asset_type = detect_asset_type(selected_ticker)
    # 기본 경로인 주식(stock)이 아닐 때만 알린다. 매 실행마다
    # "stock"이 출력되는 것을 피하기 위함.
    if asset_type.value != "stock":
        console.print(
            f"[green]감지된 자산 유형:[/green] {asset_type.value}"
        )

    # 2단계: 분석 날짜
    default_date = datetime.datetime.now().strftime("%Y-%m-%d")
    console.print(
        create_question_box(
            "2단계: 분석 날짜",
            "분석 날짜를 입력하세요 (YYYY-MM-DD)",
            default_date,
        )
    )
    analysis_date = get_analysis_date()

    # 3단계: 출력 언어 (TRADINGAGENTS_OUTPUT_LANGUAGE로 설정된 경우 건너뜀)
    if os.environ.get("TRADINGAGENTS_OUTPUT_LANGUAGE"):
        output_language = DEFAULT_CONFIG["output_language"]
        console.print(
            f"[green]✓ 환경변수에서 출력 언어 설정됨:[/green] {output_language}"
        )
    else:
        console.print(
            create_question_box(
                "3단계: 출력 언어",
                "분석가 보고서와 최종 결정에 사용할 언어를 선택하세요"
            )
        )
        output_language = ask_output_language()

    # 4단계: 애널리스트 선택
    console.print(
        create_question_box(
            "4단계: 분석가 팀", "분석에 사용할 LLM 분석가 에이전트를 선택하세요"
        )
    )
    selected_analysts = select_analysts(asset_type)
    console.print(
        f"[green]선택된 분석가:[/green] {', '.join(analyst.value for analyst in selected_analysts)}"
    )

    # 5단계: 리서치 깊이 (두 라운드 수가 모두 환경변수로 설정된 경우 건너뜀).
    # 리서치 깊이는 토론(debate) + 리스크(risk) 라운드 수에 대응한다. 둘 다
    # TRADINGAGENTS_MAX_DEBATE_ROUNDS / _MAX_RISK_ROUNDS 로 주어지면
    # 대화형 질문 없이 환경변수 값을 그대로 따른다 (#977).
    depth_from_env = bool(os.environ.get("TRADINGAGENTS_MAX_DEBATE_ROUNDS")) and bool(
        os.environ.get("TRADINGAGENTS_MAX_RISK_ROUNDS")
    )
    if depth_from_env:
        selected_research_depth = DEFAULT_CONFIG["max_debate_rounds"]
        console.print(
            f"[green]✓ 환경변수에서 리서치 깊이(Research Depth) 설정됨:[/green] "
            f"토론 {DEFAULT_CONFIG['max_debate_rounds']}라운드 / "
            f"리스크 {DEFAULT_CONFIG['max_risk_discuss_rounds']}라운드"
        )
    else:
        console.print(
            create_question_box(
                "5단계: 리서치 깊이(Research Depth)", "리서치 깊이 수준을 선택하세요"
            )
        )
        selected_research_depth = select_research_depth()

    # 6단계: LLM 공급자(provider) (TRADINGAGENTS_LLM_PROVIDER로 설정된 경우 건너뜀).
    # 백엔드 URL은 TRADINGAGENTS_LLM_BACKEND_URL이 설정돼 있으면 그 값을,
    # 아니면 공급자의 기본 엔드포인트(endpoint)를 쓴다 — 메뉴에서 골랐을 때와
    # 같은 값이다.
    provider_from_env = bool(os.environ.get("TRADINGAGENTS_LLM_PROVIDER"))
    if provider_from_env:
        selected_llm_provider = DEFAULT_CONFIG["llm_provider"].lower()
        backend_url = resolve_backend_url(
            selected_llm_provider, env_url=DEFAULT_CONFIG["backend_url"]
        )
        console.print(f"[green]✓ 환경변수에서 LLM 제공자 설정됨:[/green] {selected_llm_provider}")
        console.print(f"[green]✓ 백엔드 URL:[/green] {backend_url}")
        # 나중에 실행이 실패하지 않도록 API 키는 여전히 확인/저장한다.
        ensure_api_key(selected_llm_provider)
    else:
        console.print(
            create_question_box(
                "6단계: LLM 제공자", "LLM 제공자를 선택하세요"
            )
        )
        selected_llm_provider, backend_url = select_llm_provider()

        # 지역별 엔드포인트가 있는 공급자는 메인 드롭다운을 깔끔하게 유지하기
        # 위해 지역 선택을 별도 단계로 물어본다 (중국 본토와 국제 계정은
        # API 키를 공유할 수 없다).
        if selected_llm_provider == "qwen":
            selected_llm_provider, backend_url = ask_qwen_region()
        elif selected_llm_provider == "minimax":
            selected_llm_provider, backend_url = ask_minimax_region()
        elif selected_llm_provider == "glm":
            selected_llm_provider, backend_url = ask_glm_region()

        # 공급자를 대화형으로 골랐더라도 명시적인 환경변수 백엔드 URL을
        # 존중해, 메뉴 기본값이 덮어쓰지 않도록 한다 (#978).
        backend_url = resolve_backend_url(
            selected_llm_provider, backend_url, env_url=DEFAULT_CONFIG["backend_url"]
        )

        # 범용 OpenAI 호환(OpenAI-compatible) 엔드포인트는 기본값이 없다.
        # 메뉴에서도 환경변수에서도 주어지지 않았다면 직접 물어본다.
        if selected_llm_provider == "openai_compatible" and not backend_url:
            backend_url = prompt_openai_compatible_url()

        # Ollama의 경우 모델 선택 전에 최종 결정된 엔드포인트
        # (OLLAMA_BASE_URL vs 기본값)를 보여줘 어디에 접속하는지 분명히 한다.
        if selected_llm_provider == "ollama":
            confirm_ollama_endpoint(backend_url)

        # 공급자의 API 키가 있는지 확인한다. 없으면 사용자에게 붙여넣도록
        # 요청하고 .env에 저장해, 분석 실행이 첫 API 호출에서 실패하지 않게 한다.
        ensure_api_key(selected_llm_provider)

    # 7단계: 사고 에이전트(thinking agents) (모델 중 하나라도 환경변수로 설정되면 건너뜀)
    if os.environ.get("TRADINGAGENTS_QUICK_THINK_LLM") or os.environ.get("TRADINGAGENTS_DEEP_THINK_LLM"):
        selected_shallow_thinker = DEFAULT_CONFIG["quick_think_llm"]
        selected_deep_thinker = DEFAULT_CONFIG["deep_think_llm"]
        console.print(
            f"[green]✓ 환경변수에서 사고 에이전트(Thinking Agents) 설정됨:[/green] "
            f"quick={selected_shallow_thinker}, deep={selected_deep_thinker}"
        )
    else:
        console.print(
            create_question_box(
                "7단계: 사고 에이전트(Thinking Agents)", "분석에 사용할 사고 에이전트를 선택하세요"
            )
        )
        selected_shallow_thinker = select_shallow_thinking_agent(selected_llm_provider)
        selected_deep_thinker = select_deep_thinking_agent(selected_llm_provider)

    # 8단계: 공급자별 추론/사고 설정. 각 옵션은 해당하는 TRADINGAGENTS_*
    # 환경변수로 설정할 수 있다. 그 변수가 설정돼 있으면(또는 공급자 자체가
    # 환경변수에서 왔으면) 질문을 건너뛰고 설정된 값을 쓴다 — 위 단계들과
    # 같은 환경변수 우선 규칙이다. None = 각 공급자 자체 기본값.
    thinking_level = None
    reasoning_effort = None
    anthropic_effort = None

    provider_lower = selected_llm_provider.lower()
    if provider_from_env:
        thinking_level = DEFAULT_CONFIG["google_thinking_level"]
        reasoning_effort = DEFAULT_CONFIG["openai_reasoning_effort"]
        anthropic_effort = DEFAULT_CONFIG["anthropic_effort"]
    elif provider_lower == "google":
        thinking_level = thinking_value_or_prompt(
            "TRADINGAGENTS_GOOGLE_THINKING_LEVEL", "google_thinking_level",
            "Gemini 사고 모드", "8단계: 사고 모드(Thinking Mode)",
            "Gemini 사고 모드를 설정하세요", ask_gemini_thinking_config,
        )
    elif provider_lower == "openai":
        reasoning_effort = thinking_value_or_prompt(
            "TRADINGAGENTS_OPENAI_REASONING_EFFORT", "openai_reasoning_effort",
            "추론 강도(Reasoning Effort)", "8단계: 추론 강도(Reasoning Effort)",
            "OpenAI 추론 강도 수준을 설정하세요", ask_openai_reasoning_effort,
        )
    elif provider_lower == "anthropic":
        anthropic_effort = thinking_value_or_prompt(
            "TRADINGAGENTS_ANTHROPIC_EFFORT", "anthropic_effort",
            "Claude 강도(Effort)", "8단계: 강도 수준(Effort Level)",
            "Claude 강도 수준을 설정하세요", ask_anthropic_effort,
        )

    return {
        "ticker": selected_ticker,
        "asset_type": asset_type.value,
        "analysis_date": analysis_date,
        "analysts": selected_analysts,
        "research_depth": selected_research_depth,
        "llm_provider": selected_llm_provider.lower(),
        "backend_url": backend_url,
        "shallow_thinker": selected_shallow_thinker,
        "deep_thinker": selected_deep_thinker,
        "google_thinking_level": thinking_level,
        "openai_reasoning_effort": reasoning_effort,
        "anthropic_effort": anthropic_effort,
        "output_language": output_language,
    }


def get_analysis_date():
    """사용자 입력으로 분석 날짜를 받는다."""
    while True:
        date_str = typer.prompt(
            "", default=datetime.datetime.now().strftime("%Y-%m-%d")
        )
        try:
            # 날짜 형식을 검증하고 미래 날짜가 아닌지 확인
            analysis_date = datetime.datetime.strptime(date_str, "%Y-%m-%d")
            if analysis_date.date() > datetime.datetime.now().date():
                console.print("[red]오류: 분석 날짜는 미래일 수 없습니다[/red]")
                continue
            return date_str
        except ValueError:
            console.print(
                "[red]오류: 잘못된 날짜 형식입니다. YYYY-MM-DD 형식을 사용하세요[/red]"
            )


def save_report_to_disk(final_state, ticker: str, save_path: Path):
    """전체 분석 보고서를 디스크에 저장한다 (CLI/API 공용 저장 함수)."""
    return write_report_tree(final_state, ticker, save_path)


def display_complete_report(final_state):
    """전체 분석 보고서를 순차적으로 출력한다 (내용 잘림 방지)."""
    console.print()
    console.print(Rule("전체 분석 보고서", style="bold green"))

    # I. 애널리스트 팀 보고서
    analysts = []
    if final_state.get("market_report"):
        analysts.append(("Market Analyst", final_state["market_report"]))
    if final_state.get("sentiment_report"):
        analysts.append(("Sentiment Analyst", final_state["sentiment_report"]))
    if final_state.get("news_report"):
        analysts.append(("News Analyst", final_state["news_report"]))
    if final_state.get("fundamentals_report"):
        analysts.append(("Fundamentals Analyst", final_state["fundamentals_report"]))
    if analysts:
        console.print(Panel("[bold]I. 분석가 팀 보고서[/bold]", border_style="cyan"))
        for title, content in analysts:
            console.print(Panel(Markdown(content), title=title, border_style="blue", padding=(1, 2)))

    # II. 리서치 팀 보고서
    if final_state.get("investment_debate_state"):
        debate = final_state["investment_debate_state"]
        research = []
        if debate.get("bull_history"):
            research.append(("Bull Researcher", debate["bull_history"]))
        if debate.get("bear_history"):
            research.append(("Bear Researcher", debate["bear_history"]))
        if debate.get("judge_decision"):
            research.append(("Research Manager", debate["judge_decision"]))
        if research:
            console.print(Panel("[bold]II. 리서치 팀 결정[/bold]", border_style="magenta"))
            for title, content in research:
                console.print(Panel(Markdown(content), title=title, border_style="blue", padding=(1, 2)))

    # III. 트레이딩 팀
    if final_state.get("trader_investment_plan"):
        console.print(Panel("[bold]III. 트레이딩 팀 계획[/bold]", border_style="yellow"))
        console.print(Panel(Markdown(final_state["trader_investment_plan"]), title="Trader", border_style="blue", padding=(1, 2)))

    # IV. 리스크 관리 팀
    if final_state.get("risk_debate_state"):
        risk = final_state["risk_debate_state"]
        risk_reports = []
        if risk.get("aggressive_history"):
            risk_reports.append(("Aggressive Analyst", risk["aggressive_history"]))
        if risk.get("conservative_history"):
            risk_reports.append(("Conservative Analyst", risk["conservative_history"]))
        if risk.get("neutral_history"):
            risk_reports.append(("Neutral Analyst", risk["neutral_history"]))
        if risk_reports:
            console.print(Panel("[bold]IV. 리스크 관리 팀 결정[/bold]", border_style="red"))
            for title, content in risk_reports:
                console.print(Panel(Markdown(content), title=title, border_style="blue", padding=(1, 2)))

        # V. 포트폴리오 매니저 결정
        if risk.get("judge_decision"):
            console.print(Panel("[bold]V. 포트폴리오 매니저 최종 결정[/bold]", border_style="green"))
            console.print(Panel(Markdown(risk["judge_decision"]), title="Portfolio Manager", border_style="blue", padding=(1, 2)))


def update_research_team_status(status):
    """리서치 팀 구성원의 상태를 갱신한다 (Trader 제외)."""
    research_team = ["Bull Researcher", "Bear Researcher", "Research Manager"]
    for agent in research_team:
        message_buffer.update_agent_status(agent, status)


# 상태 전환에 쓰이는 애널리스트 순서 목록
ANALYST_ORDER = ["market", "social", "news", "fundamentals"]
ANALYST_AGENT_NAMES = {
    "market": "Market Analyst",
    "social": "Sentiment Analyst",
    "news": "News Analyst",
    "fundamentals": "Fundamentals Analyst",
}
ANALYST_REPORT_MAP = {
    "market": "market_report",
    "social": "sentiment_report",
    "news": "news_report",
    "fundamentals": "fundamentals_report",
}


def update_analyst_statuses(message_buffer, chunk, wall_time_tracker=None):
    """누적된 보고서 상태를 기준으로 애널리스트 상태를 갱신한다.

    동작 로직 (간단한 상태 머신, state machine):
    - 현재 청크(chunk)에 새 보고서 내용이 있으면 저장
    - 상태 판정은 현재 청크가 아니라 누적된 report_sections를 기준으로 함
    - 보고서가 있는 애널리스트 = completed
    - 보고서가 없는 첫 번째 애널리스트 = in_progress
    - 나머지 보고서 없는 애널리스트 = pending
    - 모든 애널리스트가 끝나면 Bull Researcher를 in_progress로 전환
    """
    selected = message_buffer.selected_analysts
    found_active = False

    if wall_time_tracker is not None:
        sync_analyst_tracker_from_chunk(wall_time_tracker, chunk)

    for analyst_key in ANALYST_ORDER:
        if analyst_key not in selected:
            continue

        agent_name = ANALYST_AGENT_NAMES[analyst_key]
        report_key = ANALYST_REPORT_MAP[analyst_key]

        # 현재 청크에서 새 보고서 내용을 수집
        if chunk.get(report_key):
            message_buffer.update_report_section(report_key, chunk[report_key])

        # 현재 청크만이 아닌 누적 섹션 기준으로 상태 판정
        has_report = bool(message_buffer.report_sections.get(report_key))

        if has_report:
            message_buffer.update_agent_status(agent_name, "completed")
        elif not found_active:
            message_buffer.update_agent_status(agent_name, "in_progress")
            found_active = True
        else:
            message_buffer.update_agent_status(agent_name, "pending")

    # 모든 애널리스트가 완료되면 리서치 팀을 in_progress로 전환
    if (
        not found_active
        and selected
        and message_buffer.agent_status.get("Bull Researcher") == "pending"
    ):
        message_buffer.update_agent_status("Bull Researcher", "in_progress")

def extract_content_string(content):
    """다양한 메시지 형식에서 문자열 내용을 추출한다.
    의미 있는 텍스트 내용이 없으면 None을 반환한다.
    """
    import ast

    def is_empty(val):
        """파이썬의 참/거짓 판정(truthiness)으로 값이 비었는지 확인한다."""
        if val is None or val == '':
            return True
        if isinstance(val, str):
            s = val.strip()
            if not s:
                return True
            try:
                return not bool(ast.literal_eval(s))
            except (ValueError, SyntaxError):
                return False  # 파싱 불가 = 실제 텍스트로 간주
        return not bool(val)

    if is_empty(content):
        return None

    if isinstance(content, str):
        return content.strip()

    if isinstance(content, dict):
        text = content.get('text', '')
        return text.strip() if not is_empty(text) else None

    if isinstance(content, list):
        text_parts = [
            item.get('text', '').strip() if isinstance(item, dict) and item.get('type') == 'text'
            else (item.strip() if isinstance(item, str) else '')
            for item in content
        ]
        result = ' '.join(t for t in text_parts if t and not is_empty(t))
        return result if result else None

    return str(content).strip() if not is_empty(content) else None


def classify_message_type(message) -> tuple[str, str | None]:
    """LangChain 메시지를 표시용 유형으로 분류하고 내용을 추출한다.

    Returns:
        (type, content) - type은 User, Agent, Data, Control 중 하나
                        - content는 추출된 문자열 또는 None
    """
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    content = extract_content_string(getattr(message, 'content', None))

    if isinstance(message, HumanMessage):
        if content and content.strip() == "Continue":
            return ("Control", content)
        return ("User", content)

    if isinstance(message, ToolMessage):
        return ("Data", content)

    if isinstance(message, AIMessage):
        return ("Agent", content)

    # 알 수 없는 유형에 대한 대비책(fallback)
    return ("System", content)


def format_tool_args(args, max_length=80) -> str:
    """도구 인자(tool arguments)를 터미널 표시용으로 포맷한다."""
    result = str(args)
    if len(result) > max_length:
        return result[:max_length - 3] + "..."
    return result

def _build_run_config(selections: dict, checkpoint: bool | None) -> dict:
    """대화형 선택 결과로 실행 설정(config)을 조립한다. 환경변수 우선 규칙을 따른다.

    라운드 수와 체크포인트(checkpoint)는 "명시적 환경변수/플래그가 이긴다" 규칙을
    따른다: DEFAULT_CONFIG에 환경변수로 적용된 값은 사용자가 CLI에서 직접
    덮어쓰지 않는 한 유지된다.
    """
    config = DEFAULT_CONFIG.copy()
    # 리서치 깊이는 두 라운드 수를 모두 설정하지만, 명시적 환경변수 오버라이드
    # (TRADINGAGENTS_MAX_DEBATE_ROUNDS / _MAX_RISK_ROUNDS)가 대화형 선택보다
    # 우선한다 — 환경변수로 적용된 값을 그대로 둔다 (#977).
    if not os.environ.get("TRADINGAGENTS_MAX_DEBATE_ROUNDS"):
        config["max_debate_rounds"] = selections["research_depth"]
    if not os.environ.get("TRADINGAGENTS_MAX_RISK_ROUNDS"):
        config["max_risk_discuss_rounds"] = selections["research_depth"]
    config["quick_think_llm"] = selections["shallow_thinker"]
    config["deep_think_llm"] = selections["deep_thinker"]
    config["backend_url"] = selections["backend_url"]
    config["llm_provider"] = selections["llm_provider"].lower()
    # 공급자별 사고(thinking) 설정
    config["google_thinking_level"] = selections.get("google_thinking_level")
    config["openai_reasoning_effort"] = selections.get("openai_reasoning_effort")
    config["anthropic_effort"] = selections.get("anthropic_effort")
    config["output_language"] = selections.get("output_language", "Korean")
    # --checkpoint/--no-checkpoint는 명시적으로 줬을 때만 덮어쓴다. 플래그를
    # 생략하면 TRADINGAGENTS_CHECKPOINT_ENABLED / 기본값이 유지된다 (#976).
    if checkpoint is not None:
        config["checkpoint_enabled"] = checkpoint
    return config


def run_analysis(checkpoint: bool | None = None):
    # 먼저 사용자 선택 사항을 모두 받는다
    selections = get_user_selections()

    config = _build_run_config(selections, checkpoint)

    # LLM/도구 호출 추적용 통계 콜백 핸들러 생성
    stats_handler = StatsCallbackHandler()

    # 애널리스트 선택을 사전 정의된 순서로 정규화 (선택은 '집합', 순서는 고정)
    selected_set = {analyst.value for analyst in selections["analysts"]}
    selected_analyst_keys = [a for a in ANALYST_ORDER if a in selected_set]
    analyst_execution_plan = build_analyst_execution_plan(selected_analyst_keys)
    analyst_wall_time_tracker = AnalystWallTimeTracker(analyst_execution_plan)

    # LLM에 콜백을 바인딩한 상태로 그래프 초기화
    graph = TradingAgentsGraph(
        selected_analyst_keys,
        config=config,
        debug=True,
        callbacks=[stats_handler],
    )

    # 선택된 애널리스트로 메시지 버퍼 초기화
    message_buffer.init_for_analysis(selected_analyst_keys)

    # 경과 시간 표시를 위한 시작 시각 기록
    start_time = time.time()

    # 결과 디렉터리 생성
    results_dir = Path(config["results_dir"]) / selections["ticker"] / selections["analysis_date"]
    results_dir.mkdir(parents=True, exist_ok=True)
    report_dir = results_dir / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    log_file = results_dir / "message_tool.log"
    log_file.touch(exist_ok=True)

    # 아래 세 데코레이터(decorator)는 message_buffer의 메서드를 감싸,
    # 화면 갱신과 동시에 로그 파일/보고서 파일에도 내용이 남도록 한다.
    def save_message_decorator(obj, func_name):
        func = getattr(obj, func_name)
        @wraps(func)
        def wrapper(*args, **kwargs):
            func(*args, **kwargs)
            timestamp, message_type, content = obj.messages[-1]
            content = content.replace("\n", " ")  # 줄바꿈을 공백으로 치환
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"{timestamp} [{message_type}] {content}\n")
        return wrapper

    def save_tool_call_decorator(obj, func_name):
        func = getattr(obj, func_name)
        @wraps(func)
        def wrapper(*args, **kwargs):
            func(*args, **kwargs)
            timestamp, tool_name, args = obj.tool_calls[-1]
            args_str = ", ".join(f"{k}={v}" for k, v in args.items())
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"{timestamp} [Tool Call] {tool_name}({args_str})\n")
        return wrapper

    def save_report_section_decorator(obj, func_name):
        func = getattr(obj, func_name)
        @wraps(func)
        def wrapper(section_name, content):
            func(section_name, content)
            if section_name in obj.report_sections and obj.report_sections[section_name] is not None:
                content = obj.report_sections[section_name]
                if content:
                    file_name = f"{section_name}.md"
                    text = "\n".join(str(item) for item in content) if isinstance(content, list) else content
                    with open(report_dir / file_name, "w", encoding="utf-8") as f:
                        f.write(text)
        return wrapper

    message_buffer.add_message = save_message_decorator(message_buffer, "add_message")
    message_buffer.add_tool_call = save_tool_call_decorator(message_buffer, "add_tool_call")
    message_buffer.update_report_section = save_report_section_decorator(message_buffer, "update_report_section")

    # 이제 화면 레이아웃을 시작한다.
    # Rich의 Live는 화면을 계속 다시 그려 주는 컨텍스트로,
    # 이 블록 안에서 layout 내용을 바꾸면 터미널에 실시간 반영된다.
    layout = create_layout()

    with Live(layout, refresh_per_second=4):
        # 초기 화면 그리기
        update_display(layout, stats_handler=stats_handler, start_time=start_time)

        # 초기 메시지 추가
        message_buffer.add_message("System", f"선택된 종목 코드: {selections['ticker']}")
        if selections["asset_type"] != "stock":
            message_buffer.add_message("System", f"감지된 자산 유형: {selections['asset_type']}")
        message_buffer.add_message(
            "System", f"분석 날짜: {selections['analysis_date']}"
        )
        message_buffer.add_message(
            "System",
            f"선택된 분석가: {', '.join(analyst.value for analyst in selections['analysts'])}",
        )
        update_display(layout, stats_handler=stats_handler, start_time=start_time)

        # 첫 번째 애널리스트의 상태를 in_progress로 갱신
        first_analyst = get_initial_analyst_node(analyst_execution_plan)
        message_buffer.update_agent_status(first_analyst, "in_progress")
        analyst_wall_time_tracker.mark_started(selected_analyst_keys[0])
        update_display(layout, stats_handler=stats_handler, start_time=start_time)

        # 스피너(spinner) 텍스트 생성
        spinner_text = (
            f"{selections['ticker']} 분석 중 ({selections['analysis_date']})..."
        )
        update_display(layout, spinner_text, stats_handler=stats_handler, start_time=start_time)

        # 상태를 초기화하고 콜백이 포함된 그래프 인자를 얻는다.
        # 종목(instrument)의 실제 정체를 여기서 한 번 확정해 모든 에이전트가
        # 실제 회사를 기준으로 삼게 한다 (#814). CLI는 propagate()를 거치지
        # 않고 상태를 직접 만들기 때문에 CLI 경로에서도 이 과정이 필요하다.
        instrument_context = graph.resolve_instrument_context(
            selections["ticker"], selections["asset_type"]
        )
        init_agent_state = graph.propagator.create_initial_state(
            selections["ticker"],
            selections["analysis_date"],
            asset_type=selections["asset_type"],
            instrument_context=instrument_context,
        )
        # 도구 실행 추적을 위해 그래프 설정에 콜백 전달
        # (LLM 추적은 LLM 생성자를 통해 별도로 처리됨)
        args = graph.propagator.get_graph_args(callbacks=[stats_handler])

        # 분석을 스트리밍으로 실행한다.
        # graph.stream()은 그래프의 노드가 실행될 때마다 부분 상태(청크)를
        # 내보내며, 아래 루프는 청크마다 메시지/상태/화면을 갱신한다.
        trace = []
        for chunk in graph.graph.stream(init_agent_state, **args):
            # 청크의 모든 메시지를 처리하되, 메시지 ID로 중복 제거
            for message in chunk.get("messages", []):
                msg_id = getattr(message, "id", None)
                if msg_id is not None:
                    if msg_id in message_buffer._processed_message_ids:
                        continue
                    message_buffer._processed_message_ids.add(msg_id)

                msg_type, content = classify_message_type(message)
                if content and content.strip():
                    message_buffer.add_message(msg_type, content)

                if hasattr(message, "tool_calls") and message.tool_calls:
                    for tool_call in message.tool_calls:
                        if isinstance(tool_call, dict):
                            message_buffer.add_tool_call(tool_call["name"], tool_call["args"])
                        else:
                            message_buffer.add_tool_call(tool_call.name, tool_call.args)

            # 보고서 상태 기준으로 애널리스트 상태 갱신 (모든 청크마다 실행)
            update_analyst_statuses(
                message_buffer,
                chunk,
                wall_time_tracker=analyst_wall_time_tracker,
            )

            # 리서치 팀 - 투자 토론 상태(investment debate state) 처리
            if chunk.get("investment_debate_state"):
                debate_state = chunk["investment_debate_state"]
                bull_hist = debate_state.get("bull_history", "").strip()
                bear_hist = debate_state.get("bear_history", "").strip()
                judge = debate_state.get("judge_decision", "").strip()

                # 실제 내용이 있을 때만 상태 갱신
                if bull_hist or bear_hist:
                    update_research_team_status("in_progress")
                if bull_hist:
                    message_buffer.update_report_section(
                        "investment_plan", f"### Bull Researcher 분석\n{bull_hist}"
                    )
                if bear_hist:
                    message_buffer.update_report_section(
                        "investment_plan", f"### Bear Researcher 분석\n{bear_hist}"
                    )
                if judge:
                    message_buffer.update_report_section(
                        "investment_plan", f"### Research Manager 결정\n{judge}"
                    )
                    update_research_team_status("completed")
                    message_buffer.update_agent_status("Trader", "in_progress")

            # 트레이딩 팀
            if chunk.get("trader_investment_plan"):
                message_buffer.update_report_section(
                    "trader_investment_plan", chunk["trader_investment_plan"]
                )
                if message_buffer.agent_status.get("Trader") != "completed":
                    message_buffer.update_agent_status("Trader", "completed")
                    message_buffer.update_agent_status("Aggressive Analyst", "in_progress")

            # 리스크 관리 팀 - 리스크 토론 상태(risk debate state) 처리
            if chunk.get("risk_debate_state"):
                risk_state = chunk["risk_debate_state"]
                agg_hist = risk_state.get("aggressive_history", "").strip()
                con_hist = risk_state.get("conservative_history", "").strip()
                neu_hist = risk_state.get("neutral_history", "").strip()
                judge = risk_state.get("judge_decision", "").strip()

                if agg_hist:
                    if message_buffer.agent_status.get("Aggressive Analyst") != "completed":
                        message_buffer.update_agent_status("Aggressive Analyst", "in_progress")
                    message_buffer.update_report_section(
                        "final_trade_decision", f"### Aggressive Analyst 분석\n{agg_hist}"
                    )
                if con_hist:
                    if message_buffer.agent_status.get("Conservative Analyst") != "completed":
                        message_buffer.update_agent_status("Conservative Analyst", "in_progress")
                    message_buffer.update_report_section(
                        "final_trade_decision", f"### Conservative Analyst 분석\n{con_hist}"
                    )
                if neu_hist:
                    if message_buffer.agent_status.get("Neutral Analyst") != "completed":
                        message_buffer.update_agent_status("Neutral Analyst", "in_progress")
                    message_buffer.update_report_section(
                        "final_trade_decision", f"### Neutral Analyst 분석\n{neu_hist}"
                    )
                if judge and message_buffer.agent_status.get("Portfolio Manager") != "completed":
                    message_buffer.update_agent_status("Portfolio Manager", "in_progress")
                    message_buffer.update_report_section(
                        "final_trade_decision", f"### Portfolio Manager 결정\n{judge}"
                    )
                    message_buffer.update_agent_status("Aggressive Analyst", "completed")
                    message_buffer.update_agent_status("Conservative Analyst", "completed")
                    message_buffer.update_agent_status("Neutral Analyst", "completed")
                    message_buffer.update_agent_status("Portfolio Manager", "completed")

            # 화면 갱신
            update_display(layout, stats_handler=stats_handler, start_time=start_time)

            trace.append(chunk)

        # 스트리밍된 청크는 전체 상태가 아니라 노드별 변경분(delta)이다.
        # 실행 전반에 걸쳐 채워진 모든 보고서 필드가 담기도록 병합한다.
        final_state = {}
        for chunk in trace:
            final_state.update(chunk)

        # 모든 에이전트 상태를 completed로 갱신
        for agent in message_buffer.agent_status:
            message_buffer.update_agent_status(agent, "completed")

        message_buffer.add_message(
            "System", f"{selections['analysis_date']} 분석이 완료되었습니다"
        )
        message_buffer.add_message("System", analyst_wall_time_tracker.format_summary())

        # 최종 보고서 섹션 갱신
        for section in message_buffer.report_sections:
            if section in final_state:
                message_buffer.update_report_section(section, final_state[section])

        update_display(layout, stats_handler=stats_handler, start_time=start_time)

    # 분석 후 질문들 (깔끔한 상호작용을 위해 Live 컨텍스트 밖에서 진행)
    console.print("\n[bold cyan]분석 완료![/bold cyan]\n")
    console.print(f"[dim]{analyst_wall_time_tracker.format_summary()}[/dim]")

    # 보고서 저장 여부 질문
    save_choice = typer.prompt("보고서를 저장할까요?", default="Y").strip().upper()
    if save_choice in ("Y", "YES", ""):
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        default_path = Path.cwd() / "reports" / f"{selections['ticker']}_{timestamp}"
        save_path_str = typer.prompt(
            "저장 경로 (기본값을 쓰려면 Enter)",
            default=str(default_path)
        ).strip()
        save_path = Path(save_path_str)
        try:
            report_file = save_report_to_disk(final_state, selections["ticker"], save_path)
            console.print(f"\n[green]✓ 보고서 저장 위치:[/green] {save_path.resolve()}")
            console.print(f"  [dim]전체 보고서:[/dim] {report_file.name}")
        except Exception as e:
            console.print(f"[red]보고서 저장 중 오류: {e}[/red]")

    # 전체 보고서 화면 표시 여부 질문
    display_choice = typer.prompt("\n전체 보고서를 화면에 표시할까요?", default="Y").strip().upper()
    if display_choice in ("Y", "YES", ""):
        display_complete_report(final_state)


@app.command()
def analyze(
    checkpoint: bool | None = typer.Option(
        None,
        "--checkpoint/--no-checkpoint",
        help="체크포인트-재개(checkpoint-resume) 활성화/비활성화 (노드마다 상태를 "
        "저장해 중단된 실행을 이어서 재개). 생략하면 TRADINGAGENTS_CHECKPOINT_ENABLED를 따릅니다.",
    ),
    clear_checkpoints: bool = typer.Option(
        False,
        "--clear-checkpoints",
        help="실행 전에 저장된 체크포인트를 모두 삭제합니다 (강제로 새로 시작).",
    ),
):
    if clear_checkpoints:
        from tradingagents.graph.checkpointer import clear_all_checkpoints
        n = clear_all_checkpoints(DEFAULT_CONFIG["data_cache_dir"])
        console.print(f"[yellow]체크포인트 {n}개를 삭제했습니다.[/yellow]")
    try:
        run_analysis(checkpoint=checkpoint)
    except _NO_CONSOLE_ERRORS:
        # 콘솔 버퍼가 없는 터미널에서는 대화형 프롬프트를 띄울 수 없다.
        # prompt_toolkit 트레이스백 대신 조치 방법이 담긴 안내 한 줄을 stderr로
        # 출력한다. rich도 렌더링되지 않을 수 있으므로 일반 텍스트를 쓴다 (#1138).
        typer.echo(
            "오류: 사용 가능한 Windows 콘솔이 없습니다. 대화형 CLI에는 실제 콘솔 "
            "버퍼가 필요합니다 — 파이프로 연결되거나 임베디드된 터미널이 아닌 "
            "Windows Terminal, PowerShell 또는 cmd.exe에서 실행하세요.",
            err=True,
        )
        raise typer.Exit(code=1) from None


if __name__ == "__main__":
    app()
