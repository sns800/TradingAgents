# [모듈 개요 - 초보자용]
# 이 파일은 애널리스트(analyst, 시장/뉴스/재무 등을 분석하는 에이전트)들의
# "실행 계획"을 정의합니다. 어떤 애널리스트들을 포함할지, 각 애널리스트가
# 그래프(graph)에서 사용하는 노드(node) 이름과 전용 메시지 채널은 무엇인지를
# 한곳에 모아 관리합니다. setup.py가 그래프를 조립할 때와 CLI가 진행 상황을
# 표시할 때 이 계획을 참조하며, 애널리스트별 실행 시간(wall time)을 재는
# 트래커(tracker)도 함께 제공합니다.
# 중기 로드맵 #6(분석가 병렬화) 이후 애널리스트들은 START에서 동시에
# 시작하므로 specs의 순서는 "실행 순서"가 아니라 표시(진행 화면·요약) 순서를
# 의미합니다.

from collections.abc import Iterable
from dataclasses import dataclass
from time import monotonic

from tradingagents.agents.utils.agent_states import ANALYST_MESSAGE_CHANNELS

# 병렬 fan-out의 합류(join) 지점 노드 이름. 모든 애널리스트 분기(도구 루프
# 포함)가 끝난 뒤 한 번만 실행되는 배리어(barrier)로, setup.py가
# defer=True로 등록하고 conditional_logic의 라우터들이 분석 종료 시 이
# 이름을 반환합니다 (중기 로드맵 #6).
ANALYST_JOIN_NODE = "Analyst Join"


# 애널리스트 한 명이 그래프에서 차지하는 노드들의 명세(spec).
# key: 내부 식별자, agent_node: 분석 담당 노드 이름,
# tool_node: 도구 호출 노드 이름, messages_key: 전용 메시지 채널 이름
# (중기 #6 — 애널리스트와 그 ToolNode는 이 채널만 읽고 씁니다),
# report_key: 최종 보고서가 저장되는 상태(state) 딕셔너리의 키.
# clear_node 필드는 중기 #6에서 제거 — 병렬화로 Msg Clear 노드 자체가
# 사라졌습니다(채널이 분리되어 다음 애널리스트를 위해 비울 대화가 없음).
@dataclass(frozen=True)
class AnalystNodeSpec:
    key: str
    agent_node: str
    tool_node: str
    messages_key: str
    report_key: str


# 선택된 애널리스트들을 담는 계획(plan) 객체. specs 순서는 표시 순서입니다.
@dataclass(frozen=True)
class AnalystExecutionPlan:
    specs: list[AnalystNodeSpec]


ANALYST_NODE_SPECS: dict[str, AnalystNodeSpec] = {
    "market": AnalystNodeSpec(
        key="market",
        agent_node="Market Analyst",
        tool_node="tools_market",
        messages_key=ANALYST_MESSAGE_CHANNELS["market"],
        report_key="market_report",
    ),
    "social": AnalystNodeSpec(
        # 저장된 설정과의 하위 호환(back-compat)을 위해 내부 키(key)는 "social"을
        # 유지합니다. 사용자에게 보이는 라벨은 v0.2.5에서 이름이 바뀐
        # "Sentiment Analyst"입니다 (sentiment_analyst는 이제 소셜 미디어만이
        # 아니라 뉴스 + StockTwits + Reddit까지 수집합니다).
        key="social",
        agent_node="Sentiment Analyst",
        tool_node="tools_social",
        messages_key=ANALYST_MESSAGE_CHANNELS["social"],
        report_key="sentiment_report",
    ),
    "news": AnalystNodeSpec(
        key="news",
        agent_node="News Analyst",
        tool_node="tools_news",
        messages_key=ANALYST_MESSAGE_CHANNELS["news"],
        report_key="news_report",
    ),
    "fundamentals": AnalystNodeSpec(
        key="fundamentals",
        agent_node="Fundamentals Analyst",
        tool_node="tools_fundamentals",
        messages_key=ANALYST_MESSAGE_CHANNELS["fundamentals"],
        report_key="fundamentals_report",
    ),
}


def build_analyst_execution_plan(
    selected_analysts: Iterable[str],
) -> AnalystExecutionPlan:
    # 사용자가 선택한 애널리스트 키 목록을 검증하고 실행 계획으로 변환합니다.
    # 알 수 없는 키가 들어오면 즉시 오류를 내서 잘못된 설정을 조기에 발견합니다.
    specs: list[AnalystNodeSpec] = []
    for analyst_key in selected_analysts:
        spec = ANALYST_NODE_SPECS.get(analyst_key)
        if spec is None:
            raise ValueError(f"unknown analyst key: {analyst_key}")
        specs.append(spec)

    if not specs:
        raise ValueError("at least one analyst must be selected")

    return AnalystExecutionPlan(specs=specs)


class AnalystWallTimeTracker:
    # 애널리스트별 실행 시간(wall time, 실제 경과 시간)을 측정하는 트래커.
    # CLI가 진행 상황 요약을 표시할 때 사용합니다.
    def __init__(self, plan: AnalystExecutionPlan):
        self.plan = plan
        self._started_at: dict[str, float] = {}
        self._wall_times: dict[str, float] = {}

    def mark_started(self, analyst_key: str, started_at: float | None = None) -> None:
        # 시작 시각을 기록합니다. setdefault를 쓰므로 이미 기록된 시작 시각은
        # 덮어쓰지 않습니다(중복 호출에 안전).
        if analyst_key not in ANALYST_NODE_SPECS:
            raise ValueError(f"unknown analyst key: {analyst_key}")
        self._started_at.setdefault(analyst_key, monotonic() if started_at is None else started_at)

    def mark_completed(
        self,
        analyst_key: str,
        completed_at: float | None = None,
    ) -> None:
        # 완료 시각을 기록하고 경과 시간을 계산합니다. 이미 완료 기록이 있거나
        # 시작 기록이 없으면 조용히 넘어갑니다.
        if analyst_key not in ANALYST_NODE_SPECS:
            raise ValueError(f"unknown analyst key: {analyst_key}")
        if analyst_key in self._wall_times:
            return
        started_at = self._started_at.get(analyst_key)
        if started_at is None:
            return
        finished_at = monotonic() if completed_at is None else completed_at
        self._wall_times[analyst_key] = max(0.0, finished_at - started_at)

    def get_wall_times(self) -> dict[str, float]:
        return dict(self._wall_times)

    def format_summary(self) -> str:
        # "Market 12.34s | News 5.67s" 형태의 사람이 읽기 좋은 요약 문자열을 만듭니다.
        parts = []
        for spec in self.plan.specs:
            duration = self._wall_times.get(spec.key)
            if duration is not None:
                label = spec.agent_node.removesuffix(" Analyst")
                parts.append(f"{label} {duration:.2f}s")
        if not parts:
            return "Analyst wall time: pending"
        return "Analyst wall time: " + " | ".join(parts)


def sync_analyst_tracker_from_chunk(
    tracker: AnalystWallTimeTracker,
    chunk: dict[str, str],
    now: float | None = None,
) -> None:
    # 그래프 스트리밍 중 도착한 상태 조각(chunk)을 보고 트래커를 갱신합니다.
    # 병렬 fan-out(중기 로드맵 #6)에서는 선택된 애널리스트 전원이 START에서
    # 동시에 시작하므로, "아직 보고서가 없는 첫 번째 애널리스트만 실행 중"
    # 이라는 예전의 직렬 가정 대신 모든 애널리스트를 시작 상태로 기록합니다
    # (mark_started는 setdefault라 최초 청크의 시각이 시작 시각으로 남습니다).
    # 보고서(report)가 채워진 애널리스트는 완료 처리합니다.
    current_time = monotonic() if now is None else now

    for spec in tracker.plan.specs:
        tracker.mark_started(spec.key, started_at=current_time)
        if chunk.get(spec.report_key):
            tracker.mark_completed(spec.key, completed_at=current_time)
