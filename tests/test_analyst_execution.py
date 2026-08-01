# 이 파일은 애널리스트(analyst) 에이전트의 실행 계획(execution plan) 생성과
# 각 애널리스트의 실행 소요 시간(wall time) 추적 기능을 검증하는 테스트 모음입니다.
# 분석가 병렬화(설계분석 중기 로드맵 #6)에 맞춰 갱신됨: clear_node(Msg Clear)가
# messages_key(전용 메시지 채널)로 대체됐고, get_initial_analyst_node는 "첫 번째
# 애널리스트"라는 직렬 개념 자체가 사라져 함께 삭제됐습니다.
import unittest

from tradingagents.graph.analyst_execution import (
    AnalystWallTimeTracker,
    build_analyst_execution_plan,
    sync_analyst_tracker_from_chunk,
)


class AnalystExecutionPlanTests(unittest.TestCase):
    def test_build_plan_preserves_selected_order(self):
        """사용자가 선택한 애널리스트 순서가 실행 계획에 그대로 유지되는지 검증하는 테스트.

        병렬화 이후 specs 순서는 실행 순서가 아니라 표시(진행 화면·요약)
        순서지만, 사용자가 고른 순서 보존이라는 계약은 그대로 유지됩니다.
        """
        plan = build_analyst_execution_plan(["news", "market"])

        self.assertEqual([spec.key for spec in plan.specs], ["news", "market"])
        self.assertEqual(plan.specs[0].agent_node, "News Analyst")
        self.assertEqual(plan.specs[0].tool_node, "tools_news")
        # clear_node 대신 전용 메시지 채널이 스펙의 일부가 됐다 (중기 #6).
        self.assertEqual(plan.specs[0].messages_key, "news_messages")

    def test_each_analyst_has_distinct_messages_key(self):
        """애널리스트마다 전용 메시지 채널이 서로 달라야(격리) 함을 검증하는 테스트 (중기 #6)."""
        plan = build_analyst_execution_plan(
            ["market", "social", "news", "fundamentals"]
        )
        keys = [spec.messages_key for spec in plan.specs]
        self.assertEqual(len(keys), len(set(keys)))
        # 내부 키 "social"의 채널 이름도 social 접두사를 유지한다 (하위 호환).
        self.assertIn("social_messages", keys)

    def test_rejects_unknown_analyst_keys(self):
        """알 수 없는 애널리스트 키가 들어오면 ValueError를 던지는지 검증하는 테스트."""
        with self.assertRaises(ValueError):
            build_analyst_execution_plan(["market", "macro"])

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

    def test_syncs_wall_time_from_parallel_chunks(self):
        """스트리밍 청크(chunk)에 담긴 보고서 완료 신호로부터 소요 시간을 동기화하는지 검증하는 테스트.

        분석가 병렬화(중기 로드맵 #6) 이후의 의미론: 선택된 애널리스트 전원이
        START에서 동시에 시작하므로, 첫 청크 시각(now=10.0)이 모두의 시작
        시각이 됩니다. 예전 직렬 의미론에서는 news가 market 완료 시점(13.0)에
        시작한 것으로 기록돼 wall time이 5.0이었지만, 병렬에서는 10.0에 시작해
        18.0에 끝났으므로 8.0이 맞습니다.
        """
        plan = build_analyst_execution_plan(["market", "news"])
        tracker = AnalystWallTimeTracker(plan)

        sync_analyst_tracker_from_chunk(tracker, {}, now=10.0)
        # 아직 완료된 애널리스트는 없지만 전원 시작 시각은 기록됐다.
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
            {"market": 3.0, "news": 8.0},
        )

    def test_sync_marks_all_selected_analysts_started_on_first_chunk(self):
        """첫 청크에서 선택된 애널리스트 전원이 시작 상태로 기록되는지 검증하는 테스트 (중기 #6).

        병렬 fan-out에서는 보고서가 없는 애널리스트도 이미 실행 중이므로,
        이후 완료 청크가 오면 첫 청크 시각 기준의 wall time이 나와야 합니다.
        """
        plan = build_analyst_execution_plan(["market", "social", "news"])
        tracker = AnalystWallTimeTracker(plan)

        sync_analyst_tracker_from_chunk(tracker, {}, now=100.0)
        # 세 애널리스트가 서로 다른 시점에 완료 — 시작은 모두 100.0이어야 한다.
        sync_analyst_tracker_from_chunk(tracker, {"sentiment_report": "d"}, now=101.0)
        sync_analyst_tracker_from_chunk(
            tracker, {"sentiment_report": "d", "market_report": "d"}, now=104.0
        )
        sync_analyst_tracker_from_chunk(
            tracker,
            {"sentiment_report": "d", "market_report": "d", "news_report": "d"},
            now=110.0,
        )
        self.assertEqual(
            tracker.get_wall_times(),
            {"social": 1.0, "market": 4.0, "news": 10.0},
        )
