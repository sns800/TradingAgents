# 이 파일은 애널리스트(analyst) 에이전트의 실행 계획(execution plan) 생성과
# 각 애널리스트의 실행 소요 시간(wall time) 추적 기능을 검증하는 테스트 모음입니다.
import unittest

from tradingagents.graph.analyst_execution import (
    AnalystWallTimeTracker,
    build_analyst_execution_plan,
    get_initial_analyst_node,
    sync_analyst_tracker_from_chunk,
)


class AnalystExecutionPlanTests(unittest.TestCase):
    def test_build_plan_preserves_selected_order(self):
        """사용자가 선택한 애널리스트 순서가 실행 계획에 그대로 유지되는지 검증하는 테스트."""
        plan = build_analyst_execution_plan(["news", "market"])

        self.assertEqual([spec.key for spec in plan.specs], ["news", "market"])
        self.assertEqual(plan.specs[0].agent_node, "News Analyst")
        self.assertEqual(plan.specs[0].tool_node, "tools_news")
        self.assertEqual(plan.specs[0].clear_node, "Msg Clear News")

    def test_rejects_unknown_analyst_keys(self):
        """알 수 없는 애널리스트 키가 들어오면 ValueError를 던지는지 검증하는 테스트."""
        with self.assertRaises(ValueError):
            build_analyst_execution_plan(["market", "macro"])

    def test_get_initial_analyst_node_uses_plan_metadata(self):
        """실행 계획의 메타데이터를 이용해 첫 번째 애널리스트 노드(node)를 올바르게 찾는지 검증하는 테스트."""
        plan = build_analyst_execution_plan(["fundamentals", "news"])

        self.assertEqual(
            get_initial_analyst_node(plan),
            "Fundamentals Analyst",
        )

    def test_social_key_displays_as_sentiment_analyst(self):
        """내부 키 "social"이 화면에는 "Sentiment Analyst"로 표시되는지 검증하는 테스트."""
        # 저장된 설정과의 하위 호환성(back-compat)을 위해 내부 전송 키는 "social"을
        # 유지하지만, 사용자에게 보이는 agent_node 라벨은 v0.2.5 이름 변경에 맞춰야
        # 합니다. 즉 실행 시간 요약이나 agent_node를 소비하는 미래의 코드는 기존
        # "Social Analyst" 대신 "Sentiment Analyst"라고 표기해야 합니다.
        plan = build_analyst_execution_plan(["social"])
        spec = plan.specs[0]
        self.assertEqual(spec.key, "social")
        self.assertEqual(spec.agent_node, "Sentiment Analyst")
        self.assertEqual(spec.report_key, "sentiment_report")


class AnalystWallTimeTrackerTests(unittest.TestCase):
    def test_records_wall_time_when_analyst_completes(self):
        """애널리스트가 완료되면 실제 소요 시간(wall time)이 기록되는지 검증하는 테스트."""
        plan = build_analyst_execution_plan(["market", "news"])
        tracker = AnalystWallTimeTracker(plan)

        tracker.mark_started("market", started_at=10.0)
        tracker.mark_completed("market", completed_at=13.5)

        self.assertEqual(tracker.get_wall_times(), {"market": 3.5})

    def test_formats_summary_in_plan_order(self):
        """소요 시간 요약이 완료 순서가 아닌 실행 계획 순서대로 출력되는지 검증하는 테스트."""
        plan = build_analyst_execution_plan(["news", "market"])
        tracker = AnalystWallTimeTracker(plan)

        tracker.mark_started("market", started_at=20.0)
        tracker.mark_completed("market", completed_at=22.25)
        tracker.mark_started("news", started_at=10.0)
        tracker.mark_completed("news", completed_at=14.0)

        self.assertEqual(
            tracker.format_summary(),
            "Analyst wall time: News 4.00s | Market 2.25s",
        )

    def test_syncs_wall_time_from_sequential_chunks(self):
        """스트리밍 청크(chunk)에 담긴 보고서 완료 신호로부터 소요 시간을 동기화하는지 검증하는 테스트."""
        plan = build_analyst_execution_plan(["market", "news"])
        tracker = AnalystWallTimeTracker(plan)

        sync_analyst_tracker_from_chunk(tracker, {}, now=10.0)
        self.assertEqual(tracker.get_wall_times(), {})

        sync_analyst_tracker_from_chunk(
            tracker,
            {"market_report": "done"},
            now=13.0,
        )
        self.assertEqual(tracker.get_wall_times(), {"market": 3.0})

        sync_analyst_tracker_from_chunk(
            tracker,
            {"market_report": "done", "news_report": "done"},
            now=18.0,
        )
        self.assertEqual(
            tracker.get_wall_times(),
            {"market": 3.0, "news": 5.0},
        )
