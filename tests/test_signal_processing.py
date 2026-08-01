"""[모듈 개요] 공용 등급(rating) 파싱 휴리스틱(heuristic)과 SignalProcessor
어댑터(adapter)를 검증하는 테스트.

포트폴리오 매니저(Portfolio Manager)는 구조화 출력(structured output)으로
타입이 지정된 PortfolioDecision을 생성하고, 이를 항상 ``**Rating**: X`` 헤더가
포함된 마크다운으로 렌더링한다. 따라서 ``tradingagents.agents.utils.rating``의
결정적(deterministic) 휴리스틱만으로 하류(downstream)에서 등급을 추출하기에
충분하며 — 두 번째 LLM 호출이 필요 없다 — SignalProcessor는 이제 그 휴리스틱에
위임하는 얇은 어댑터다.
"""

import pytest

from tradingagents.agents.utils.rating import RATINGS_5_TIER, parse_rating
from tradingagents.graph.signal_processing import SignalProcessor

# ---------------------------------------------------------------------------
# 휴리스틱 파서(parser)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestParseRating:
    def test_explicit_label_buy(self):
        """'Rating: Buy' 형태의 명시적 라벨을 파싱하는지 검증하는 테스트."""
        assert parse_rating("Rating: Buy\nReasoning here.") == "Buy"

    def test_explicit_label_overweight(self):
        """'Rating: Overweight' 라벨을 파싱하는지 검증하는 테스트."""
        assert parse_rating("Rating: Overweight\nDetails.") == "Overweight"

    def test_explicit_label_with_markdown_bold_value(self):
        """등급 값이 마크다운 굵게(**) 표시로 감싸여 있어도 파싱되는지 검증하는 테스트."""
        # 회귀(regression) 방지: Rating: **Sell** — 값 주위에 마크다운이 있는 경우.
        assert parse_rating("Rating: **Sell**\nExit immediately.") == "Sell"

    def test_explicit_label_with_markdown_bold_label(self):
        """라벨 쪽이 마크다운 굵게 표시(**Rating**)여도 파싱되는지 검증하는 테스트."""
        assert parse_rating("**Rating**: Underweight\nTrim exposure.") == "Underweight"

    def test_rendered_pm_markdown_shape(self):
        """render_pm_decision이 생성하는 정확한 마크다운 형태가 항상 파싱되는지 검증하는 테스트."""
        # render_pm_decision이 만들어 내는 정확한 형태는 항상 파싱돼야 한다.
        text = (
            "**Rating**: Buy\n\n"
            "**Executive Summary**: Enter at $189-192, 6% portfolio cap.\n\n"
            "**Investment Thesis**: AI capex cycle intact; institutional flows constructive."
        )
        assert parse_rating(text) == "Buy"

    def test_explicit_label_wins_over_prose_with_markdown(self):
        """본문 서술에 등급 단어가 섞여 있어도 명시적 라벨이 우선하는지 검증하는 테스트."""
        text = (
            "The buy thesis is weakened by guidance.\n"
            "Rating: **Sell**\n"
            "Exit before earnings."
        )
        assert parse_rating(text) == "Sell"

    def test_no_rating_returns_default(self):
        """등급이 없으면 기본값(Hold)을 반환하는지 검증하는 테스트."""
        assert parse_rating("No clear directional signal at this time.") == "Hold"

    def test_no_rating_custom_default(self):
        """사용자 지정 기본값이 지정되면 그 값을 반환하는지 검증하는 테스트."""
        assert parse_rating("Plain prose.", default="Underweight") == "Underweight"

    def test_all_five_tiers_recognised(self):
        """5단계 등급 체계의 모든 등급이 인식되는지 검증하는 테스트."""
        for r in RATINGS_5_TIER:
            assert parse_rating(f"Rating: {r}") == r


# ---------------------------------------------------------------------------
# SignalProcessor: 휴리스틱을 감싸는 얇은 어댑터(adapter)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSignalProcessor:
    def test_returns_rating_from_pm_markdown(self):
        """포트폴리오 매니저 마크다운에서 등급을 추출하는지 검증하는 테스트."""
        sp = SignalProcessor()
        md = "**Rating**: Overweight\n\n**Executive Summary**: Build gradually."
        assert sp.process_signal(md) == "Overweight"

    def test_makes_no_llm_calls(self):
        """SignalProcessor가 생성 시 받은 LLM을 호출하지 않는지 검증하는 테스트.

        등급은 렌더링된 포트폴리오 매니저 마크다운에서 직접 파싱할 수 있으므로
        LLM 호출이 없어야 한다.
        """
        from unittest.mock import MagicMock

        llm = MagicMock()
        sp = SignalProcessor(llm)
        sp.process_signal("Rating: Buy\nDetails.")
        llm.invoke.assert_not_called()
        llm.with_structured_output.assert_not_called()

    def test_default_when_no_rating_present(self):
        """등급이 없는 본문에서 기본값(Hold)을 반환하는지 검증하는 테스트."""
        sp = SignalProcessor()
        assert sp.process_signal("Plain prose without a recommendation.") == "Hold"


@pytest.mark.unit
class TestKoreanRatingParsing:
    """한국어 결정문에서의 등급 추출 (설계분석 단기 로드맵 #1).

    이 포크는 기본 출력 언어가 한국어라, 구조화 출력 실패 시 자유 텍스트
    결정문이 한국어로 생성된다. 영어 어휘만 알던 파서는 그 경우 무조건
    기본값 Hold를 반환해 실제 결정(예: Sell)을 조용히 뒤집었다.
    """

    def test_korean_label_and_value(self):
        """"등급: 매도" 형태를 Sell로 추출하는지 검증하는 테스트."""
        assert parse_rating("등급: 매도\n근거는 다음과 같습니다.") == "Sell"

    def test_korean_label_with_markdown_bold(self):
        """"**등급**: 비중축소" 형태를 Underweight로 추출하는지 검증하는 테스트."""
        assert parse_rating("**등급**: 비중축소\n포지션을 줄이세요.") == "Underweight"

    def test_english_label_korean_value(self):
        """"Rating: 매수" (영어 라벨 + 한국어 값) 조합을 검증하는 테스트."""
        assert parse_rating("**Rating**: 매수\n상승 여력이 큽니다.") == "Buy"

    def test_all_korean_tiers_recognised(self):
        """한국어 5단계 어휘가 모두 표준 영어 등급으로 매핑되는지 검증하는 테스트."""
        pairs = [
            ("매수", "Buy"), ("비중확대", "Overweight"), ("보유", "Hold"),
            ("비중축소", "Underweight"), ("매도", "Sell"),
        ]
        for ko, expected in pairs:
            assert parse_rating(f"등급: {ko}") == expected

    def test_prose_fallback_korean_token(self):
        """라벨 없이 본문의 독립된 한국어 등급 단어를 인식하는지 검증하는 테스트."""
        assert parse_rating("결론적으로 **매도** 판단입니다.") == "Sell"

    def test_spaced_two_word_form(self):
        """"비중 확대"처럼 띄어 쓴 형태를 인식하는지 검증하는 테스트."""
        assert parse_rating("포트폴리오에서 비중 축소를 권고합니다.") == "Underweight"

    def test_compound_word_not_mistaken(self):
        """"매도세" 같은 복합어가 등급으로 오인되지 않는지 검증하는 테스트."""
        assert parse_rating("매도세가 강하지만 아직 판단 보류.") == "Hold"

    def test_missing_rating_logs_warning(self, caplog):
        """등급 어휘가 없으면 기본값과 함께 경고를 남기는지 검증하는 테스트.

        조용한 Hold 고착은 시그널을 무의미하게 만들므로 반드시 로그 흔적이
        필요하다 (설계분석 발견 #2).
        """
        import logging

        with caplog.at_level(logging.WARNING, logger="tradingagents.agents.utils.rating"):
            result = parse_rating("등급 어휘가 전혀 없는 문장.", context="unit test")
        assert result == "Hold"
        assert any("no rating vocabulary" in r.message and "unit test" in r.message
                   for r in caplog.records)
