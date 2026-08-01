# =============================================================================
# [모듈 개요 — 초보자용]
# 이 파일은 TradingAgents(LLM 멀티 에이전트 주식 트레이딩 프레임워크)의 공용
# 유틸리티 모음입니다. 두 가지 역할을 합니다:
# 1) 각 데이터 툴 파일에 흩어져 있는 LangChain 툴들을 한곳에서 재수출(re-export)해서
#    에이전트와 그래프가 모두 이 모듈에서 import하도록 하는 공개 창구 역할.
# 2) 분석 대상 종목의 정체성(instrument identity) 확인, 출력 언어 지시문,
#    메시지 정리(delete) 헬퍼 등 에이전트 실행에 필요한 보조 함수 제공.
# =============================================================================

import functools
import logging
from collections.abc import Mapping
from typing import Any

import yfinance as yf
from langchain_core.messages import HumanMessage, RemoveMessage

# 별도 유틸리티 파일들에서 툴을 가져온다(재수출용)
from tradingagents.agents.utils.core_stock_tools import get_stock_data
from tradingagents.agents.utils.fundamental_data_tools import (
    get_balance_sheet,
    get_cashflow,
    get_fundamentals,
    get_income_statement,
)
from tradingagents.agents.utils.macro_data_tools import get_macro_indicators
from tradingagents.agents.utils.market_data_validation_tools import get_verified_market_snapshot
from tradingagents.agents.utils.news_data_tools import (
    get_global_news,
    get_insider_transactions,
    get_news,
)
from tradingagents.agents.utils.prediction_markets_tools import get_prediction_markets
from tradingagents.agents.utils.technical_indicators_tools import get_indicators

# 공개 인터페이스(public surface): 에이전트와 그래프가 데이터 툴을 한곳에서
# import할 수 있도록 여기에 모아두고, 아래에 정의된 종목 정보/언어 헬퍼도 포함한다.
__all__ = [
    "get_stock_data",
    "get_indicators",
    "get_fundamentals",
    "get_balance_sheet",
    "get_cashflow",
    "get_income_statement",
    "get_news",
    "get_global_news",
    "get_insider_transactions",
    "get_macro_indicators",
    "get_prediction_markets",
    "get_verified_market_snapshot",
    "build_instrument_context",
    "resolve_instrument_identity",
    "get_instrument_context_from_state",
    "get_language_instruction",
    "get_verified_snapshot_block",
    "create_msg_delete",
]

logger = logging.getLogger(__name__)


def get_language_instruction() -> str:
    """설정된 출력 언어에 맞는 프롬프트 지시문을 반환한다.

    영어(기본값)일 때는 빈 문자열을 반환해 불필요한 토큰을 쓰지 않는다.
    저장되는 리포트에 출력이 반영되는 모든 에이전트(분석가, 리서처, 토론자,
    리서치 매니저, 트레이더, 포트폴리오 매니저)에 적용되어, 영어가 아닌
    언어로 실행할 때 언어가 뒤섞이지 않고 완전히 현지화된 리포트가 나온다.
    """
    from tradingagents.dataflows.config import get_config
    lang = get_config().get("output_language", "English")
    if lang.strip().lower() == "english":
        return ""
    return f" Write your entire response in {lang}."


def get_verified_snapshot_block(state: Mapping[str, Any]) -> str:
    """하류 프롬프트에 넣을 검증 스냅샷 섹션을 반환한다. 스냅샷이 없으면 빈 문자열.

    [검증 스냅샷 보존 — 설계분석 중기 로드맵 #5] Msg Clear가 원본 도구
    데이터를 파기한 뒤에도 하류 에이전트(리서처/리스크 토론자, 트레이더,
    리서치 매니저, 포트폴리오 매니저)가 정확한 가격·지표 수치의 기준점을
    갖도록, 시장 분석가가 보존한 스냅샷(verified_snapshot 상태 필드)을
    프롬프트 섹션으로 감쌉니다. 스냅샷은 수백 자 수준의 마크다운 표라
    전문을 그대로 주입해도 토큰 부담이 작습니다. 비어 있으면(스냅샷 도구
    미호출, NO_DATA, 구형 체크포인트) 섹션 전체를 생략해 빈 섹션이
    존재하지 않는 수치를 지어내게 유도하지 않습니다(past_context의 빈 값
    가드와 동일한 패턴). 지시문은 LLM 프롬프트이므로 영어를 유지합니다.
    """
    snapshot = state.get("verified_snapshot", "")
    if not isinstance(snapshot, str) or not snapshot.strip():
        return ""
    return (
        "Verified market snapshot (authoritative numbers — when citing exact "
        "prices or indicator values, cite them from here; do not invent "
        f"figures):\n{snapshot}\n\n"
    )


def _clean_identity_value(value: Any) -> str | None:
    """공백을 제거한 문자열을 반환하고, 빈 값이나 자리표시자(placeholder)성 값이면 None을 반환한다."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or cleaned.lower() in {"none", "n/a", "nan", "null"}:
        return None
    return cleaned


@functools.lru_cache(maxsize=256)
def resolve_instrument_identity(ticker: str) -> dict:
    """티커(ticker)의 결정론적 정체성 메타데이터(회사명, 섹터 등)를 확인한다.

    이 함수는 차트 패턴이 실제와 다른 업종을 암시할 때 파이프라인이 *다른*
    회사를 지어내는(환각, hallucination) 문제를 막기 위해 존재한다(#814):
    확정된(ground-truth) 회사명이 없으면 시장 분석가가 가격 움직임을
    그럴듯한 서사에 끼워 맞춰 엉뚱한 정체성을 만들어내고, 그것이 이후의
    모든 하위 에이전트로 연쇄 전파된다.

    설계상 최선 노력(best-effort) 방식: yfinance를 쓸 수 없거나, 요청 제한
    (rate limit)에 걸리거나, 티커를 인식하지 못하면 ``{}``를 반환하고 호출자는
    분석 시작 전에 실패하는 대신 티커만 담긴 컨텍스트로 대체(fallback)한다.
    캐시(lru_cache)를 적용해 프로세스당 티커별로 최대 한 번만 조회한다.

    심볼은 먼저 정규화(normalize)된다(예: ``XAUUSD`` -> ``GC=F``). 이렇게 해야
    가격 조회 경로가 실제로 가져오는 것과 같은 종목의 정체성을 확인한다(#983).
    """
    from tradingagents.dataflows.symbol_utils import normalize_symbol

    try:
        info = yf.Ticker(normalize_symbol(ticker)).info or {}
    except Exception as exc:  # noqa: BLE001 — 실패해도 열어둔다(fail open), 실행을 절대 막지 않음
        logger.debug("Could not resolve instrument identity for %s: %s", ticker, exc)
        return {}

    identity: dict[str, str] = {}
    company_name = _clean_identity_value(info.get("longName")) or _clean_identity_value(
        info.get("shortName")
    )
    if company_name:
        identity["company_name"] = company_name
    for source_key, target_key in (
        ("sector", "sector"),
        ("industry", "industry"),
        ("exchange", "exchange"),
        ("quoteType", "quote_type"),
    ):
        value = _clean_identity_value(info.get(source_key))
        if value:
            identity[target_key] = value
    return identity


def build_instrument_context(
    ticker: str,
    asset_type: str = "stock",
    identity: Mapping[str, str] | None = None,
) -> str:
    """분석 대상 종목을 정확히 서술해 에이전트가 정체성과 티커를 유지하게 한다.

    ``identity``가 주어지면(:func:`resolve_instrument_identity`로 결정론적으로
    확인된 값), 회사명과 업종 분류를 컨텍스트에 주입해 에이전트가 가격 차트를
    엉뚱한 회사에 끼워 맞추지 않고 실제 회사에 고정(anchor)되도록 한다(#814).
    """
    # 아래 문자열들은 LLM 프롬프트에 그대로 들어가므로 영어 원문을 유지한다.
    is_crypto = asset_type == "crypto"
    instrument_label = "asset" if is_crypto else "instrument"
    context = (
        f"The {instrument_label} to analyze is `{ticker}`. "
        "Use this exact ticker in every tool call, report, and recommendation, "
        "preserving any exchange suffix (e.g. `.TO`, `.L`, `.HK`, `.T`, `-USD`)."
    )

    details = []
    if identity:
        name = identity.get("company_name") or identity.get("name")
        if name:
            details.append(f"{'Name' if is_crypto else 'Company'}: {name}")
        sector, industry = identity.get("sector"), identity.get("industry")
        if sector and industry:
            details.append(f"Business classification: {sector} / {industry}")
        elif sector:
            details.append(f"Sector: {sector}")
        elif industry:
            details.append(f"Industry: {industry}")
        if identity.get("exchange"):
            details.append(f"Exchange: {identity['exchange']}")

    if details:
        context += (
            f" Resolved identity: {'; '.join(details)}. "
            "Do not substitute a different company or ticker unless a tool "
            "result explicitly disproves this resolved identity."
        )

    if is_crypto:
        context += (
            " Treat it as a crypto asset rather than a company, and do not "
            "assume company fundamentals are available."
        )
    return context


def get_instrument_context_from_state(state: Mapping[str, Any]) -> str:
    """현재 실행(run)에 해당하는 종목 컨텍스트를 반환한다.

    실행 시작 시 한 번 계산되어 상태(state)에 저장된 정체성 확인 컨텍스트를
    우선 사용한다(``TradingAgentsGraph.resolve_instrument_context`` 참고).
    상태가 그 값 없이 만들어진 경우(프로그래밍 방식의 최소 상태, 테스트)에는
    네트워크 조회 없이 티커만 담긴 컨텍스트로 대체하므로, 그래프 실행 중간에
    yfinance 호출을 강제당하는 일이 없다.
    """
    context = state.get("instrument_context")
    if isinstance(context, str) and context.strip():
        return context
    return build_instrument_context(
        str(state["company_of_interest"]),
        state.get("asset_type", "stock"),
    )


def create_msg_delete():
    def delete_messages(state):
        """메시지들을 비우고 컨텍스트에 고정된 자리표시자(placeholder)를 추가한다.

        자리표시자는 단순한 ``"Continue"``여서는 안 된다: 일부 OpenAI 호환
        공급자(provider)는 그것을 문자 그대로 사용자 과제로 해석해 종목 분석
        대신 "continue"라는 단어에 대한 출력을 생성한다(#888). 확인된 종목
        컨텍스트와 날짜에 자리표시자를 고정해 두면, 공급자가 자리표시자를
        독립된 요청으로 취급하더라도 다음 분석가가 과제에서 벗어나지 않는다.
        """
        messages = state["messages"]
        removal_operations = [RemoveMessage(id=m.id) for m in messages]

        instrument_context = get_instrument_context_from_state(state)
        trade_date = state.get("trade_date", "the requested date")
        placeholder = HumanMessage(
            content=(
                f"Proceed with your assigned analysis for this workflow. "
                f"{instrument_context} The analysis date is {trade_date}."
            )
        )
        return {"messages": removal_operations + [placeholder]}

    return delete_messages
