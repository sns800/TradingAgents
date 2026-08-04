# =============================================================================
# [모듈 개요 — 초보자용]
# 이 파일은 TradingAgents(LLM 멀티 에이전트 주식 트레이딩 프레임워크)의 공용
# 유틸리티 모음입니다. 두 가지 역할을 합니다:
# 1) 각 데이터 툴 파일에 흩어져 있는 LangChain 툴들을 한곳에서 재수출(re-export)해서
#    에이전트와 그래프가 모두 이 모듈에서 import하도록 하는 공개 창구 역할.
# 2) 분석 대상 종목의 정체성(instrument identity) 확인, 출력 언어 지시문,
#    검증 스냅샷 프롬프트 블록 등 에이전트 실행에 필요한 보조 함수 제공.
# =============================================================================

import functools
import logging
from collections.abc import Mapping
from typing import Any

import yfinance as yf

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
    "get_horizon_instruction",
    "get_instrument_context_from_state",
    "get_language_instruction",
    "get_verified_snapshot_block",
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


def get_horizon_instruction() -> str:
    """평가 지평(evaluation horizon) 지시문을 반환한다.

    [시계 정합 — 작업이력 21] 리서처는 다년 성장 논거를 펴고, 등급 척도에는
    기간 정의가 없고, 리플렉션은 holding_days(기본 5거래일) 알파로 채점하는
    시계 불일치가 있었다. 등급을 내거나 등급의 근거를 생산하는 프롬프트
    (리서처 토론·리서치 매니저·트레이더·포트폴리오 매니저)에 이 지시문을
    공통 주입해, 판단·논거·채점이 같은 지평 위에 서게 한다. 지평은 config의
    holding_days에서 읽으므로 설정을 바꾸면 프롬프트도 따라간다.

    [지시문 한국어 요약] "이 파이프라인의 결정은 향후 약 {N}거래일의 리스크
    조정 초과수익으로 채점된다. 그 지평 위에서 판단하라 — 각 촉매와 리스크는
    그 안에 주가를 움직일 수 있는지로 가중하고, 장기 논지는 시장이 그 안에
    가격에 반영하기 시작할 개연성이 있는 만큼만 유효하며, 핵심 논거가 지평
    밖에서만 실현된다면 그렇다고 명시하라."
    """
    from tradingagents.dataflows.config import get_config
    days = get_config().get("holding_days", 5)
    return (
        f"Evaluation horizon: decisions in this pipeline are scored on risk-adjusted "
        f"excess return over roughly the next {days} trading days. Frame your judgment "
        f"on that horizon — weigh each catalyst and risk by whether it can plausibly "
        f"move the price within it, treat longer-term theses as relevant only insofar "
        f"as the market is likely to begin pricing them within the horizon, and say so "
        f"explicitly when a key argument pays off only beyond it."
    )


def get_verified_snapshot_block(state: Mapping[str, Any]) -> str:
    """하류 프롬프트에 넣을 검증 스냅샷 섹션을 반환한다. 스냅샷이 없으면 빈 문자열.

    [검증 스냅샷 보존 — 설계분석 중기 로드맵 #5] 하류 에이전트(리서처/
    리스크 토론자, 트레이더, 리서치 매니저, 포트폴리오 매니저)는 시장
    분석가의 전용 메시지 채널을 읽지 않으므로, 정확한 가격·지표 수치의
    기준점을 갖도록 시장 분석가가 보존한 스냅샷(verified_snapshot 상태
    필드)을 프롬프트 섹션으로 감쌉니다. 스냅샷은 수백 자 수준의 마크다운 표라
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


# (제거됨) create_msg_delete — 분석가 병렬화(설계분석 중기 로드맵 #6)로
# Msg Clear 노드가 불필요해져 삭제했습니다. 분석가마다 전용 메시지 채널을
# 쓰므로 "다음 분석가를 위해 공유 대화를 비우는" 우회책이 사라졌고, 그
# 과정에서 원본 도구 데이터가 파기되던 문제(설계분석-보고서 2.2절)도 함께
# 해소됐습니다. 예전 #888 자리표시자 이슈(placeholder가 "Continue"면 일부
# 공급자가 오작동)는 Msg Clear 자체가 없어졌으므로 더 이상 해당되지 않습니다.
