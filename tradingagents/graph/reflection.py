# TradingAgents/graph/reflection.py
#
# [모듈 개요 - 초보자용]
# 이 파일은 리플렉션(reflection, 과거 결정을 돌아보며 교훈을 뽑는 과정)을
# 담당합니다. 매매 결정이 내려지고 며칠 뒤 실제 수익률이 확인되면,
# LLM에게 "그때의 판단이 맞았는가, 무엇을 배울 것인가"를 2~4문장으로
# 요약하게 시킵니다. 이 요약은 메모리 로그(memory log)에 저장되어
# 다음 분석 때 과거 맥락(past_context)으로 에이전트 프롬프트에 다시
# 주입됩니다. trading_graph.py의 _resolve_pending_entries()가 호출합니다.

from typing import Any


class Reflector:
    """매매 결정에 대한 리플렉션(반성/복기)을 담당하는 클래스."""

    def __init__(self, quick_thinking_llm: Any):
        """리플렉터를 LLM과 함께 초기화한다."""
        self.quick_thinking_llm = quick_thinking_llm
        self.log_reflection_prompt = self._get_log_reflection_prompt()

    def _get_log_reflection_prompt(self) -> str:
        """reflect_on_final_decision(Phase B 로그 기록)용의 간결한 프롬프트.

        2~4문장의 평문(plain prose)을 만들어 냅니다. 이 정도로 짧아야
        나중에 에이전트 프롬프트에 다시 주입해도 컨텍스트 윈도(context
        window)를 불필요하게 부풀리지 않습니다.
        """
        # [프롬프트 한국어 요약] 결과(수익률)를 알게 된 시점에서 과거의 자기
        # 매매 결정을 복기하는 애널리스트 역할 지시입니다. 다음을 순서대로
        # 다루는 2~4문장의 평문을 요구합니다: (1) 방향성 판단이 맞았는지
        # (알파 수치 인용), (2) 투자 논지 중 어떤 부분이 유효했거나 무너졌는지,
        # (3) 다음 유사 분석에 적용할 구체적 교훈 한 가지.
        # LLM 프롬프트이므로 원문(영어)을 그대로 유지합니다.
        return (
            "You are a trading analyst reviewing your own past decision now that the outcome is known.\n"
            "Write exactly 2-4 sentences of plain prose (no bullets, no headers, no markdown).\n\n"
            "Cover in order:\n"
            "1. Was the directional call correct? (cite the alpha figure)\n"
            "2. Which part of the investment thesis held or failed?\n"
            "3. One concrete lesson to apply to the next similar analysis.\n\n"
            "Be specific and terse. Your output will be stored verbatim in a decision log "
            "and re-read by future analysts, so every word must earn its place."
        )

    def reflect_on_final_decision(
        self,
        final_decision: str,
        raw_return: float,
        alpha_return: float,
        benchmark_name: str = "SPY",
    ) -> str:
        """최종 매매 결정에 대해 결과(수익률) 맥락을 곁들여 리플렉션을 한 번 수행한다.

        Phase B의 지연 리플렉션(deferred reflection, 결과가 확정된 뒤에야
        수행하는 복기)에서 사용됩니다. final_trade_decision에는 이미 모든
        애널리스트의 통찰이 종합되어 있으므로 별도의 시장 컨텍스트는 필요
        없습니다. ``benchmark_name``은 알파(alpha, 벤치마크 대비 초과 수익)
        표기에 쓰이는 라벨입니다(예: 미국 티커는 ``"SPY"``, ``.T`` 도쿄
        상장 종목은 ``"^N225"``). 벤치마크를 넘겨주지 않는 기존 호출자를
        위해 기본값은 SPY입니다.
        """
        messages = [
            ("system", self.log_reflection_prompt),
            (
                "human",
                (
                    f"Raw return: {raw_return:+.1%}\n"
                    f"Alpha vs {benchmark_name}: {alpha_return:+.1%}\n\n"
                    f"Final Decision:\n{final_decision}"
                ),
            ),
        ]
        return self.quick_thinking_llm.invoke(messages).content
