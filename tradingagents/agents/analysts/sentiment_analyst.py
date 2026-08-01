# ============================================================================
# 감성 분석가(Sentiment Analyst) 모듈
#
# 이 에이전트는 뉴스 헤드라인, StockTwits, Reddit 등 여러 소스의 데이터를 종합해
# 대상 종목에 대한 시장 심리(sentiment)를 분석하고 점수화된 보고서를 작성합니다.
# 전체 파이프라인(분석가 → 리서처 토론 → 트레이더 → 리스크 토론 → 포트폴리오 매니저)에서
# 가장 앞 단계인 "분석가" 팀에 속하며, 여기서 만든 보고서(sentiment_report)는
# 이후 강세론자(Bull)/약세론자(Bear) 토론의 근거 자료로 사용됩니다.
# ============================================================================

"""감성 분석가(Sentiment Analyst) — 대상 종목에 대한 다중 소스 감성 분석.

이전 이름은 ``social_media_analyst`` 였습니다. 예전 버전은 프롬프트로는
소셜 미디어 분석을 요구하면서 실제 사용 가능한 도구는 Yahoo Finance 뉴스뿐이어서,
프롬프트 압박에 밀린 LLM이 Reddit/X/StockTwits 콘텐츠를 지어내는(fabricate)
문제가 실제로 확인되어 이름을 바꾸고 재설계했습니다.

재설계된 에이전트는 LLM을 호출하기 전에 서로 보완적인 세 가지 데이터 소스를
미리 가져와(pre-fetch) 구조화된 블록 형태로 프롬프트에 주입합니다:

  1. 뉴스 헤드라인        — Yahoo Finance (기관 관점의 프레이밍)
  2. StockTwits 메시지    — 캐시태그(cashtag)로 색인된 개인 트레이더 게시글,
                            사용자가 직접 붙인 Bullish/Bearish 감성 태그 포함
  3. Reddit 게시글        — r/wallstreetbets, r/stocks, r/investing

이 에이전트는 도구 호출(tool-calling)을 사용하지 않습니다. 데이터는 첫 턴부터
프롬프트 안에 들어 있습니다. 출력은 구조화 출력(structured output) 패턴을
사용하며(OpenAI/xAI는 json_schema, Gemini는 response_schema, Anthropic은
tool-use), 네이티브 지원이 없는 제공자(provider)에서는 자유 텍스트 생성으로
대체(fallback)됩니다. 덕분에 감성 헤더(밴드 + 점수 + 신뢰도)가 모델별
자유 서술이 아니라 실행/제공자에 관계없이 결정론적으로(deterministic) 나옵니다.

참고: https://github.com/TauricResearch/TradingAgents/issues/557
참고: https://github.com/TauricResearch/TradingAgents/issues/796
"""

from datetime import datetime, timedelta

from langchain_core.messages import AIMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.schemas import SentimentReport, render_sentiment_report
from tradingagents.agents.utils.agent_utils import (
    get_instrument_context_from_state,
    get_language_instruction,
    get_news,
)
from tradingagents.agents.utils.structured import (
    NO_EXTERNAL_TOOLS,
    bind_structured,
    invoke_structured_or_freetext,
)
from tradingagents.dataflows.reddit import fetch_reddit_posts
from tradingagents.dataflows.stocktwits import fetch_stocktwits_messages


def _seven_days_back(trade_date: str) -> str:
    """거래일(trade_date)로부터 7일 전 날짜를 'YYYY-MM-DD' 문자열로 반환합니다."""
    return (datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")


def create_sentiment_analyst(llm):
    """트레이딩 그래프에 넣을 감성 분석가 노드를 생성합니다.

    뉴스 + StockTwits + Reddit 데이터를 미리 가져와(pre-fetch) 구조화된
    블록으로 프롬프트에 주입하고, 구조화 출력(structured output)을 통해
    결정론적인 감성 보고서를 생성합니다(구조화 출력을 지원하지 않는
    제공자를 위한 자유 텍스트 대체 경로 포함).
    """
    # 구조화 출력 바인딩: LLM이 SentimentReport 스키마 형태로 응답하도록 감쌉니다.
    structured_llm = bind_structured(llm, SentimentReport, "Sentiment Analyst")

    # LangGraph 노드 함수: 상태(state) 딕셔너리를 입력받아
    # 갱신할 키들만 담은 딕셔너리를 반환합니다.
    def sentiment_analyst_node(state):
        ticker = state["company_of_interest"]  # 분석 대상 종목 티커
        end_date = state["trade_date"]  # 분석 기준일(거래일)
        start_date = _seven_days_back(end_date)  # 분석 시작일(7일 전)
        instrument_context = get_instrument_context_from_state(state)  # 종목/자산 정보 문자열

        # 세 가지 소스를 모두 미리 가져옵니다(pre-fetch). 각 페처(fetcher)는
        # 실패해도 우아하게 대체 문자열을 반환하므로(예외가 여기까지 올라오지 않음),
        # LLM은 항상 무언가를 보게 됩니다 — 실제 데이터이거나 명확한 자리표시자입니다.
        news_block = get_news.func(ticker, start_date, end_date)
        stocktwits_block = fetch_stocktwits_messages(ticker, limit=30)
        reddit_block = fetch_reddit_posts(ticker)

        system_message = _build_system_message(
            ticker=ticker,
            start_date=start_date,
            end_date=end_date,
            news_block=news_block,
            stocktwits_block=stocktwits_block,
            reddit_block=reddit_block,
        )

        # [한국어 요약] 아래 공통 시스템 프롬프트는 LLM에게 다음을 지시합니다:
        # "당신은 다른 어시스턴트들과 협업하는 AI다. 오늘 날짜를 '현재'로
        # 간주하라. 외부 도구는 사용하지 말라(NO_EXTERNAL_TOOLS)."
        # ※ 매수/매도 최종 제안 지시는 넣지 않습니다 — 분석가는 파이프라인
        #   1단계로 보고서만 작성하며, 최종 결정은 하류(트레이더·포트폴리오
        #   매니저)의 역할입니다.
        # ※ 프롬프트를 번역하면 모델 출력 형식이 깨질 수 있어 영어 원문을 유지합니다.
        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    # 여기서는 도구 호출을 사용하지 않습니다: 데이터가 이미 프롬프트에
                    # 주입되어 있으므로, 도구 관련 문구를 넣으면 오히려 환각(hallucination)
                    # 도구 호출을 유발할 수 있습니다(#1130).
                    " Today's date is {current_date}; treat it as 'now' for all analysis. {instrument_context}"
                    " " + NO_EXTERNAL_TOOLS +
                    "\n{system_message}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        # 프롬프트 템플릿의 자리표시자에 실제 값을 부분 적용(partial)합니다.
        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(current_date=end_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        # 템플릿을 구체적인 메시지 리스트로 포맷하여 구조화 경로와 자유 텍스트
        # 경로가 동일한 입력을 받도록 합니다. bind_tools는 사용하지 않습니다 —
        # 데이터가 이미 프롬프트 안에 있기 때문입니다.
        # 전용 채널(social_messages)만 읽습니다 — 분석가 병렬화(중기 로드맵
        # #6)로 다른 분석가의 대화와 섞이지 않습니다.
        formatted_messages = prompt.format_messages(messages=state["social_messages"])

        # 구조화 출력을 우선 시도하고, 실패하면 자유 텍스트로 대체(fallback)합니다.
        report_text = invoke_structured_or_freetext(
            structured_llm,
            llm,
            formatted_messages,
            render_sentiment_report,
            "Sentiment Analyst",
        )

        # 상태(state) 갱신: 전용 채널에 결과를 추가하고,
        # 완성된 보고서를 "sentiment_report" 키에 저장해 후속 단계에서 사용하게 합니다.
        # (도구 호출이 없는 AIMessage이므로 라우터가 곧바로 합류 노드로 보냅니다.)
        return {
            "social_messages": [AIMessage(content=report_text)],
            "sentiment_report": report_text,
        }

    return sentiment_analyst_node


def _build_system_message(
    *,
    ticker: str,
    start_date: str,
    end_date: str,
    news_block: str,
    stocktwits_block: str,
    reddit_block: str,
) -> str:
    """미리 가져온 구조화 데이터 블록을 넣어 감성 분석가용 시스템 메시지를 조립합니다."""
    # [한국어 요약] 아래 f-string 프롬프트는 LLM에게 다음을 지시합니다:
    # "당신은 금융 시장 감성 분석가다. 프롬프트에 미리 포함된 세 가지 데이터 소스
    # (Yahoo Finance 뉴스 / StockTwits 메시지 / Reddit 게시글)를 바탕으로
    # {ticker}에 대한 {start_date}~{end_date} 기간의 종합 감성 보고서를 작성하라.
    # 분석 모범 사례: StockTwits의 강세(Bullish)/약세(Bearish) 비율을 선행 지표로 읽고,
    # 소스 간 괴리(divergence)를 신호로 해석하며, Reddit 글은 참여도(추천/댓글 수)로
    # 가중하고, 의견과 사건(event)을 구분하고, 반복되는 내러티브 주제를 찾고,
    # 데이터 한계를 솔직히 밝히고, 촉매(catalyst)와 리스크를 식별하며,
    # 과거 감성이 예측력을 갖지 않음을 유의하라.
    # 출력 필드: overall_band(감성 밴드), overall_score(0~10 점수),
    # confidence(신뢰도 low/medium/high), narrative(소스별 상세 서술 + 요약 표)."
    # ※ 프롬프트를 번역하면 모델 출력 형식이 깨질 수 있어 영어 원문을 유지합니다.
    return f"""You are a financial market sentiment analyst. Your task is to produce a comprehensive sentiment report for {ticker} covering the period from {start_date} to {end_date}, drawing on three complementary data sources that have already been collected for you.

## Data sources (pre-fetched, in this prompt)

### News headlines — Yahoo Finance, past 7 days
Institutional framing. Fact-driven, slower-moving signal.

<start_of_news>
{news_block}
<end_of_news>

### StockTwits messages — retail-trader social platform indexed by cashtag
Fast-moving signal. Each message carries a user-labeled sentiment tag (Bullish / Bearish / no-label) plus the message body.

<start_of_stocktwits>
{stocktwits_block}
<end_of_stocktwits>

### Reddit posts — r/wallstreetbets, r/stocks, r/investing (past 7 days)
Community discussion. Engagement signal via upvote score and comment count. Subreddit character matters (r/wallstreetbets is often contrarian/exuberant; r/stocks more measured; r/investing longer-term).

<start_of_reddit>
{reddit_block}
<end_of_reddit>

## How to analyze this data (best practices)

1. **Read the StockTwits Bullish/Bearish ratio as a leading retail-sentiment signal.** A 70/30 bullish/bearish split is moderately bullish; ≥90/10 may indicate over-extension and contrarian risk; 50/50 is uncertainty. Sample size matters — base rates on the actual message count, not percentages alone.

2. **Look for cross-source divergences.** If news framing is bearish but StockTwits is overwhelmingly bullish, that mismatch is itself a signal — it can mean retail is leaning into a thesis the news flow hasn't caught up to (or vice versa, that retail is chasing while institutions are cautious).

3. **Weight Reddit posts by engagement.** A 400-upvote / 200-comment thread reflects community attention; a 3-upvote post is noise. Read the body excerpts for context — the title alone often misleads.

4. **Distinguish opinion from event.** A news headline ("Nvidia announces $500M Corning deal") is an event; a StockTwits post ("buying NVDA, this is going to moon") is opinion. Both are inputs but should be weighted differently in your conclusions.

5. **Identify recurring narrative themes.** What topic keeps coming up across sources? That's the dominant narrative driving current sentiment.

6. **Be honest about data limits.** If StockTwits returned only a handful of messages, or one or more sources returned an "<unavailable>" placeholder, the sentiment read is less robust — flag this explicitly in the `confidence` field and the narrative. If the sources are silent on a given subreddit, say so.

7. **Identify catalysts and risks** that emerge across sources — news of upcoming earnings, product launches, competitive threats, macro headlines, etc.

8. **Past sentiment is not predictive.** Frame your conclusions as signal for the trader to weigh alongside fundamentals and technicals, not as a price call.

## Output fields

Fill the following fields:

- **overall_band**: Exactly one of Bullish / Mildly Bullish / Neutral / Mixed / Mildly Bearish / Bearish. Use Mixed when sources point in clearly different directions; Neutral is a legitimate call when the overall signal is weak, sparse, or non-committal — do not force a directional band onto faint evidence.
- **overall_score**: A number from 0 (maximally bearish) to 10 (maximally bullish); 5 is neutral. Keep it consistent with overall_band.
- **confidence**: low / medium / high, based on data quality and sample size.
- **narrative**: Full source-by-source breakdown, divergences, dominant narrative themes, catalysts and risks, and a markdown summary table of key sentiment signals (direction, source, supporting evidence).

{get_language_instruction()}"""


# ---------------------------------------------------------------------------
# 하위 호환성(backwards-compatibility) 심(shim)
# ---------------------------------------------------------------------------
def create_social_media_analyst(llm):
    """:func:`create_sentiment_analyst` 의 사용 중단(deprecated)된 별칭입니다.

    ``create_social_media_analyst`` 를 임포트하는 기존 코드가 계속
    동작하도록 유지합니다.

    .. deprecated::
        대신 :func:`create_sentiment_analyst` 를 직접 임포트하세요.
    """
    import warnings
    warnings.warn(
        "create_social_media_analyst is deprecated and will be removed in a "
        "future version. Use create_sentiment_analyst instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    return create_sentiment_analyst(llm)
