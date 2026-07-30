# =============================================================================
# [모듈 개요 - 초보자용]
# 구조화 출력(structured output)을 내는 에이전트들이 쓰는 Pydantic 스키마 모음
# 입니다. 이 파일 자체는 에이전트가 아니라 "출력 양식 정의"이며, 전체 파이프라인
# 「분석가 → 리서처 토론 → 트레이더 → 리스크 토론 → 포트폴리오 매니저」에서
# 의사결정을 내리는 세 에이전트(리서치 매니저, 트레이더, 포트폴리오 매니저)와
# 심리 분석가(Sentiment Analyst)의 출력 형식을 담당합니다.
# 주의: 각 클래스의 docstring과 Field(description=...)는 LLM에게 전달되는 출력
# 지시문이므로 영어 원문을 유지하고, 대신 한국어 설명을 주석으로 덧붙였습니다.
# =============================================================================

"""Pydantic schemas used by agents that produce structured output.

The framework's primary artifact is still prose: each agent's natural-language
reasoning is what users read in the saved markdown reports and what the
downstream agents read as context.  Structured output is layered onto the
three decision-making agents (Research Manager, Trader, Portfolio Manager)
so that:

- Their outputs follow consistent section headers across runs and providers
- Each provider's native structured-output mode is used (json_schema for
  OpenAI/xAI, response_schema for Gemini, tool-use for Anthropic)
- Schema field descriptions become the model's output instructions, freeing
  the prompt body to focus on context and the rating-scale guidance
- A render helper turns the parsed Pydantic instance back into the same
  markdown shape the rest of the system already consumes, so display,
  memory log, and saved reports keep working unchanged
"""
# [위 docstring 한국어 요약] 이 프레임워크의 1차 산출물은 여전히 자연어 보고서
# 이지만, 세 의사결정 에이전트에는 구조화 출력을 덧입혔습니다. 목적은:
# (1) 실행/프로바이더가 달라도 일관된 섹션 헤더 유지,
# (2) 각 프로바이더의 네이티브 구조화 출력 모드 활용(OpenAI/xAI는 json_schema,
#     Gemini는 response_schema, Anthropic은 도구 호출(tool-use)),
# (3) 스키마 필드 description이 곧 모델의 출력 지시문이 되어 프롬프트 본문은
#     맥락과 등급 기준 안내에 집중,
# (4) 렌더 헬퍼가 파싱된 Pydantic 객체를 기존 시스템이 소비하던 마크다운 형태로
#     되돌려 화면 표시·메모리 로그·저장 보고서가 그대로 동작.

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# LLM은 선택적(optional) 숫자 필드를 비워 두는 대신 자리표시자 문자열("None",
# "N/A" 등)을 써넣을 때가 있습니다. 이를 None으로 강제 변환(coerce)해서 구조화
# 호출이 오류 대신 정상 검증되게 합니다(#1058). 진짜 숫자 문자열("189.5")은
# Pydantic이 여전히 float로 파싱합니다.
_NULLISH_FLOAT = {"", "none", "n/a", "na", "null", "nil", "-", "tbd", "unknown"}


def _coerce_optional_float(value):
    if isinstance(value, str) and value.strip().lower() in _NULLISH_FLOAT:
        return None
    return value


# ---------------------------------------------------------------------------
# 공용 등급 타입 (Shared rating types)
# ---------------------------------------------------------------------------


# [한국어 설명] 리서치 매니저와 포트폴리오 매니저가 쓰는 5단계 투자 등급.
# 매수(Buy) / 비중확대(Overweight) / 보유(Hold) / 비중축소(Underweight) / 매도(Sell).
# 아래 docstring과 값 문자열은 LLM 스키마와 다운스트림 파서에 쓰이므로 영어 유지.
class PortfolioRating(str, Enum):
    """5-tier rating used by the Research Manager and Portfolio Manager."""

    BUY = "Buy"
    OVERWEIGHT = "Overweight"
    HOLD = "Hold"
    UNDERWEIGHT = "Underweight"
    SELL = "Sell"


# [한국어 설명] 트레이더(Trader)가 쓰는 3단계 매매 방향: 매수(Buy) / 보유(Hold) /
# 매도(Sell). 트레이더의 역할은 리서치 매니저의 투자 계획을 구체적인 거래 제안
# 으로 바꾸는 것이고, 포지션 크기 조절이나 비중확대/축소 같은 세밀한 판단은
# 이후 포트폴리오 매니저 단계에서 이뤄집니다.
class TraderAction(str, Enum):
    """3-tier transaction direction used by the Trader.

    The Trader's job is to translate the Research Manager's investment plan
    into a concrete transaction proposal: should the desk execute a Buy, a
    Sell, or sit on Hold this round.  Position sizing and the nuanced
    Overweight / Underweight calls happen later at the Portfolio Manager.
    """

    BUY = "Buy"
    HOLD = "Hold"
    SELL = "Sell"


# ---------------------------------------------------------------------------
# 리서치 매니저 (Research Manager)
# ---------------------------------------------------------------------------


# [한국어 설명] 리서치 매니저가 강세론자(Bull)/약세론자(Bear) 토론을 종합해
# 만드는 구조화된 투자 계획. 트레이더에게 넘기는 인수인계 문서로,
# recommendation은 방향성 판단, rationale은 어느 쪽 논리가 이겼는지의 근거,
# strategic_actions는 트레이더가 실행할 구체적 지침입니다.
class ResearchPlan(BaseModel):
    """Structured investment plan produced by the Research Manager.

    Hand-off to the Trader: the recommendation pins the directional view,
    the rationale captures which side of the bull/bear debate carried the
    argument, and the strategic actions translate that into concrete
    instructions the trader can execute against.
    """

    # [한국어] 투자 추천 등급. 5개 중 정확히 하나. 양쪽 근거가 정말 팽팽할 때만
    # Hold를 쓰고, 아니면 더 강한 쪽에 확실히 손을 들라는 지시.
    recommendation: PortfolioRating = Field(
        description=(
            "The investment recommendation. Exactly one of Buy / Overweight / "
            "Hold / Underweight / Sell. Reserve Hold for situations where the "
            "evidence on both sides is genuinely balanced; otherwise commit to "
            "the side with the stronger arguments."
        ),
    )
    # [한국어] 토론 양측 핵심 논점 요약과 최종 추천에 이른 근거. 동료에게
    # 말하듯 자연스러운 대화체로 작성하라는 지시.
    rationale: str = Field(
        description=(
            "Conversational summary of the key points from both sides of the "
            "debate, ending with which arguments led to the recommendation. "
            "Speak naturally, as if to a teammate."
        ),
    )
    # [한국어] 트레이더가 추천을 실행에 옮길 구체적 단계. 등급과 일관된
    # 포지션 크기(position sizing) 가이드를 포함하라는 지시.
    strategic_actions: str = Field(
        description=(
            "Concrete steps for the trader to implement the recommendation, "
            "including position sizing guidance consistent with the rating."
        ),
    )


def render_research_plan(plan: ResearchPlan) -> str:
    """ResearchPlan을 저장용·트레이더 프롬프트 컨텍스트용 마크다운으로 렌더링한다."""
    return "\n".join([
        f"**Recommendation**: {plan.recommendation.value}",
        "",
        f"**Rationale**: {plan.rationale}",
        "",
        f"**Strategic Actions**: {plan.strategic_actions}",
    ])


# ---------------------------------------------------------------------------
# 트레이더 (Trader)
# ---------------------------------------------------------------------------


# [한국어 설명] 트레이더가 만드는 구조화된 거래 제안. 리서치 매니저의 투자
# 계획과 분석가 보고서를 읽고 구체적 거래로 변환합니다: 어떤 행동(매수/보유/
# 매도)을 취할지, 그 근거, 그리고 진입가·손절가·포지션 크기 같은 실무 수치.
class TraderProposal(BaseModel):
    """Structured transaction proposal produced by the Trader.

    The trader reads the Research Manager's investment plan and the analyst
    reports, then turns them into a concrete transaction: what action to
    take, the reasoning that justifies it, and the practical levels for
    entry, stop-loss, and sizing.
    """

    # [한국어] 매매 방향. 매수(Buy) / 보유(Hold) / 매도(Sell) 중 정확히 하나.
    action: TraderAction = Field(
        description="The transaction direction. Exactly one of Buy / Hold / Sell.",
    )
    # [한국어] 이 행동의 근거. 분석가 보고서와 리서치 계획에 기반해 2~4문장.
    reasoning: str = Field(
        description=(
            "The case for this action, anchored in the analysts' reports and "
            "the research plan. Two to four sentences."
        ),
    )
    # [한국어] 선택 항목: 진입가 목표 (해당 종목의 호가 통화 기준).
    entry_price: float | None = Field(
        default=None,
        description="Optional entry price target in the instrument's quote currency.",
    )
    # [한국어] 선택 항목: 손절가(stop-loss) (해당 종목의 호가 통화 기준).
    stop_loss: float | None = Field(
        default=None,
        description="Optional stop-loss price in the instrument's quote currency.",
    )
    # [한국어] 선택 항목: 포지션 크기 가이드. 예: '포트폴리오의 5%'.
    position_sizing: str | None = Field(
        default=None,
        description="Optional sizing guidance, e.g. '5% of portfolio'.",
    )

    # 검증 전(mode="before") 훅: LLM이 숫자 필드에 "N/A" 같은 자리표시자
    # 문자열을 넣으면 None으로 바꿔 검증 오류를 방지합니다.
    @field_validator("entry_price", "stop_loss", mode="before")
    @classmethod
    def _nullish_float_to_none(cls, v):
        return _coerce_optional_float(v)


def render_trader_proposal(proposal: TraderProposal) -> str:
    """TraderProposal을 마크다운으로 렌더링한다.

    말미의 ``FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL**`` 줄은 분석가
    중단 신호(stop-signal) 텍스트 및 이 문구를 grep하는 외부 코드와의
    하위 호환성을 위해 그대로 유지한다.
    """
    parts = [
        f"**Action**: {proposal.action.value}",
        "",
        f"**Reasoning**: {proposal.reasoning}",
    ]
    if proposal.entry_price is not None:
        parts.extend(["", f"**Entry Price**: {proposal.entry_price}"])
    if proposal.stop_loss is not None:
        parts.extend(["", f"**Stop Loss**: {proposal.stop_loss}"])
    if proposal.position_sizing:
        parts.extend(["", f"**Position Sizing**: {proposal.position_sizing}"])
    parts.extend([
        "",
        f"FINAL TRANSACTION PROPOSAL: **{proposal.action.value.upper()}**",
    ])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 포트폴리오 매니저 (Portfolio Manager)
# ---------------------------------------------------------------------------


# [한국어 설명] 파이프라인 마지막 단계인 포트폴리오 매니저의 구조화 출력(최종
# 결정). 모델이 1차 LLM 호출에서 모든 필드를 직접 채우므로 별도의 추출 단계가
# 필요 없고, 필드 description이 곧 출력 지시문 역할을 겸합니다.
class PortfolioDecision(BaseModel):
    """Structured output produced by the Portfolio Manager.

    The model fills every field as part of its primary LLM call; no separate
    extraction pass is required. Field descriptions double as the model's
    output instructions, so the prompt body only needs to convey context and
    the rating-scale guidance.
    """

    # [한국어] 최종 포지션 등급. 리스크 토론을 바탕으로 5개 중 정확히 하나 선택.
    rating: PortfolioRating = Field(
        description=(
            "The final position rating. Exactly one of Buy / Overweight / Hold / "
            "Underweight / Sell, picked based on the analysts' debate."
        ),
    )
    # [한국어] 진입 전략·포지션 크기·핵심 리스크 수준·투자 기간을 담은
    # 간결한 실행 계획 요약. 2~4문장.
    executive_summary: str = Field(
        description=(
            "A concise action plan covering entry strategy, position sizing, "
            "key risk levels, and time horizon. Two to four sentences."
        ),
    )
    # [한국어] 애널리스트 토론의 구체적 근거에 기반한 상세한 투자 논거.
    # 프롬프트에 과거 교훈(메모리)이 있으면 반영하라는 지시 포함.
    investment_thesis: str = Field(
        description=(
            "Detailed reasoning anchored in specific evidence from the analysts' "
            "debate. If prior lessons are referenced in the prompt context, "
            "incorporate them; otherwise rely solely on the current analysis."
        ),
    )
    # [한국어] 선택 항목: 목표 주가 (해당 종목의 호가 통화 기준).
    price_target: float | None = Field(
        default=None,
        description="Optional target price in the instrument's quote currency.",
    )
    # [한국어] 선택 항목: 권장 보유 기간. 예: '3-6개월'.
    time_horizon: str | None = Field(
        default=None,
        description="Optional recommended holding period, e.g. '3-6 months'.",
    )

    # 검증 전(mode="before") 훅: LLM이 숫자 필드에 "N/A" 같은 자리표시자
    # 문자열을 넣으면 None으로 바꿔 검증 오류를 방지합니다.
    @field_validator("price_target", mode="before")
    @classmethod
    def _nullish_float_to_none(cls, v):
        return _coerce_optional_float(v)


def render_pm_decision(decision: PortfolioDecision) -> str:
    """PortfolioDecision을 시스템의 나머지 부분이 기대하는 마크다운 형태로 되돌린다.

    메모리 로그, CLI 화면 표시, 저장 보고서 파일이 모두 이 마크다운을 읽으므로,
    렌더링 결과는 다운스트림 파서와 보고서 작성기가 이미 처리하고 있는 섹션
    헤더(``**Rating**``, ``**Executive Summary**``, ``**Investment Thesis**``)를
    정확히 그대로 유지한다.
    """
    parts = [
        f"**Rating**: {decision.rating.value}",
        "",
        f"**Executive Summary**: {decision.executive_summary}",
        "",
        f"**Investment Thesis**: {decision.investment_thesis}",
    ]
    if decision.price_target is not None:
        parts.extend(["", f"**Price Target**: {decision.price_target}"])
    if decision.time_horizon:
        parts.extend(["", f"**Time Horizon**: {decision.time_horizon}"])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 심리 분석가 (Sentiment Analyst)
# ---------------------------------------------------------------------------


# [한국어 설명] 심리 분석가가 산출하는 이산적(discrete) 심리 방향 6단계:
# 강세(Bullish) / 약강세(Mildly Bullish) / 중립(Neutral) / 혼재(Mixed) /
# 약약세(Mildly Bearish) / 약세(Bearish). 신호가 유의미할 만큼 세분화하면서도
# 모든 LLM 프로바이더가 JSON 출력에서 안정적으로 매핑할 수 있는 크기입니다.
class SentimentBand(str, Enum):
    """Discrete sentiment direction produced by the Sentiment Analyst.

    Six tiers keep the signal granular enough to be actionable while remaining
    small enough for every provider to map reliably from its JSON output.
    """

    BULLISH = "Bullish"
    MILDLY_BULLISH = "Mildly Bullish"
    NEUTRAL = "Neutral"
    MIXED = "Mixed"
    MILDLY_BEARISH = "Mildly Bearish"
    BEARISH = "Bearish"


# [한국어 설명] 심리 분석가가 만드는 구조화된 심리 보고서. 과거의 자유 서술형
# 출력을 대체해, 다운스트림 소비자(대시보드, 감사 로그, PDF 렌더러, 다른
# 에이전트)가 모델 릴리스마다 어긋나는 취약한 정규식(regex) 대신 overall_band와
# overall_score를 바로 읽을 수 있게 합니다. narrative에는 소스별 상세 분석이
# 보존되고, render_sentiment_report가 결정적(deterministic) 헤더를 앞에 붙여
# 저장된 보고서도 사람이 읽기 좋게 유지합니다.
class SentimentReport(BaseModel):
    """Structured sentiment report produced by the Sentiment Analyst.

    Replaces the previous free-form prose output so downstream consumers
    (dashboards, audit logs, PDF renderers, other agents) can read
    ``overall_band`` and ``overall_score`` without maintaining fragile regex
    fallbacks that drift with every model release. ``narrative`` preserves the
    rich source-by-source analysis; ``render_sentiment_report`` prepends a
    deterministic header so the saved report stays human-readable.
    """

    # [한국어] 전체 심리 방향. 6개 등급 중 정확히 하나. 소스들이 서로 다른
    # 방향을 가리키면 Mixed, 모든 소스가 무의미할 때만 Neutral을 쓰라는 지시.
    overall_band: SentimentBand = Field(
        description=(
            "Overall sentiment direction. Exactly one of: "
            "Bullish / Mildly Bullish / Neutral / Mixed / Mildly Bearish / Bearish. "
            "Use Mixed when sources point in clearly different directions. "
            "Use Neutral only when all sources are genuinely silent or non-committal."
        ),
    )
    # [한국어] 0~10 척도의 심리 강도 점수. 0=극단적 약세, 5=중립, 10=극단적
    # 강세. overall_band와의 일관성을 위한 구간 가이드가 있으나 실제로
    # 강제되는 것은 0~10 범위(ge/le)뿐입니다.
    overall_score: float = Field(
        ge=0.0,
        le=10.0,
        description=(
            "Numeric sentiment intensity on a 0–10 scale. "
            "0 = maximally bearish, 5 = neutral, 10 = maximally bullish. "
            "Guideline for consistency with overall_band: "
            "Bullish ~6.5–10, Mildly Bullish ~5.5–6.4, Neutral/Mixed ~4.5–5.5, "
            "Mildly Bearish ~3.5–4.4, Bearish ~0–3.4. "
            "Only the 0–10 bounds are enforced."
        ),
    )
    # [한국어] 평가 신뢰도(low/medium/high). 데이터 품질과 표본 크기 기준:
    # 자리표시자 응답이나 5개 미만 데이터면 low, 있지만 빈약하면 medium,
    # 세 소스 모두 실질 데이터를 반환했으면 high.
    confidence: Literal["low", "medium", "high"] = Field(
        description=(
            "Confidence in the assessment based on data quality and sample size. "
            "Use 'low' when one or more sources returned a placeholder or fewer "
            "than 5 data points; 'medium' when data is present but sparse; "
            "'high' when all three sources returned substantive data."
        ),
    )
    # [한국어] 심리 보고서 본문. 순서대로 (1) 소스별 상세 분석(메시지 수·비율·
    # 주목할 게시물 인용), (2) 소스 간 불일치와 일치점, (3) 지배적 내러티브
    # 주제, (4) 데이터가 드러낸 촉매와 리스크, (5) 핵심 심리 신호 요약
    # 마크다운 표를 포함하라는 지시.
    narrative: str = Field(
        description=(
            "Full sentiment report covering, in order: "
            "(1) source-by-source breakdown with specific evidence (cite message "
            "counts, ratios, notable posts); "
            "(2) cross-source divergences and alignments; "
            "(3) dominant narrative themes; "
            "(4) catalysts and risks surfaced by the data; "
            "(5) a markdown table summarising key sentiment signals, their "
            "direction, source, and supporting evidence. "
            "Keep it informative and substantive: develop each section thoroughly "
            "with concrete evidence so every point adds new signal for the trader."
        ),
    )


def render_sentiment_report(report: SentimentReport) -> str:
    """SentimentReport를 시스템의 나머지 부분이 기대하는 마크다운 형태로 렌더링한다.

    구조화된 헤더(방향 등급 + 점수 + 신뢰도)를 narrative 앞에 붙여, 저장된
    보고서가 사람이 읽기에도 좋고 정규식 없이 기계가 파싱하기에도 좋게 만든다.
    """
    return "\n".join([
        f"**Overall Sentiment:** **{report.overall_band.value}** "
        f"(Score: {report.overall_score:.1f}/10)",
        f"**Confidence:** {report.confidence.capitalize()}",
        "",
        report.narrative,
    ])
