# =============================================================================
# [모듈 개요 - 초보자용]
# 이 파일은 TradingAgents(LLM 멀티 에이전트 주식 트레이딩 프레임워크)의
# 구조화 출력(structured output) 기능이 실제 LLM 제공자(provider)와 잘 동작하는지
# 확인하는 스모크 테스트(smoke test) 스크립트입니다.
# 전체 그래프를 돌리지 않고, 의사결정 에이전트 3개(리서치 매니저, 트레이더,
# 포트폴리오 매니저)만 직접 실행해 결과 형식이 올바른지 검사합니다.
#
# 아래 docstring은 argparse의 --help 설명문(description)으로 그대로 출력되므로
# 영어 원문을 유지합니다. 내용 요약:
#   - 세 의사결정 에이전트를 구조화 출력 바인딩(binding)으로 직접 실행하고,
#     타입이 지정된 Pydantic 인스턴스와 렌더링된 마크다운(markdown)을 출력한다.
#   - 각 제공자의 네이티브 구조화 출력 모드(OpenAI/xAI/DeepSeek/Qwen/GLM은
#     json_schema, Gemini는 response_schema, Anthropic은 tool-use)가
#     우리가 정의한 스키마(schema)대로 깨끗한 인스턴스를 반환하는지 검증한다.
#   - 비용을 아끼기 위해 propagate()는 호출하지 않으며, 구조화 출력 호출 3건과
#     휴리스틱(heuristic) SignalProcessor만 실행한다.
#   - 사용법: 제공자별 API 키 환경변수를 설정한 뒤
#     `python scripts/smoke_structured_output.py <provider>` 형태로 실행.
# =============================================================================
"""End-to-end smoke for structured-output agents against a real LLM provider.

Runs the three decision-making agents (Research Manager, Trader, Portfolio
Manager) directly with their structured-output bindings and prints the
typed Pydantic instance + the rendered markdown for each.  Use this to
verify a provider's native structured-output mode (json_schema for
OpenAI / xAI / DeepSeek / Qwen / GLM, response_schema for Gemini, tool-use
for Anthropic) returns clean instances on the schemas we ship.

Usage:
    OPENAI_API_KEY=... python scripts/smoke_structured_output.py openai
    GOOGLE_API_KEY=... python scripts/smoke_structured_output.py google
    ANTHROPIC_API_KEY=... python scripts/smoke_structured_output.py anthropic
    DEEPSEEK_API_KEY=... python scripts/smoke_structured_output.py deepseek

The script does NOT call propagate(), to keep the surface tight and the
cost low — it exercises only the three structured-output calls we just
added, plus the heuristic SignalProcessor.
"""

from __future__ import annotations

import argparse
import sys

from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
from tradingagents.agents.managers.research_manager import create_research_manager
from tradingagents.agents.trader.trader import create_trader
from tradingagents.graph.signal_processing import SignalProcessor
from tradingagents.llm_clients import create_llm_client

# 제공자(provider)별 기본 모델 이름 매핑
PROVIDER_DEFAULTS = {
    "openai": ("gpt-5.4-mini", None),
    "google": ("gemini-3.5-flash", None),
    "anthropic": ("claude-sonnet-4-6", None),
    "deepseek": ("deepseek-v4-flash", None),
    "qwen": ("qwen3.7-plus", None),
    "glm": ("glm-5", None),
    "xai": ("grok-4.3", None),
}


# 세 에이전트에게 넘길 최소한이지만 현실적인 상태(state) 데이터.
# 아래 영어 문자열은 LLM에 입력되는 토론 내용(프롬프트 재료)이므로 원문 유지.
# 내용 요약: 강세론자(Bull)는 NVDA의 데이터센터 매출 성장과 국가 단위 AI 수주를,
# 약세론자(Bear)는 고객 집중 위험과 중국 수출 규제를 근거로 논쟁하는 기록이다.
DEBATE_HISTORY = """
Bull Analyst: NVDA's data-center revenue grew 60% YoY last quarter, driven by
Blackwell ramp; sovereign AI deals with multiple governments add a $40B+
multi-year tailwind. Margins remain above peer average.

Bear Analyst: Concentration risk is real — top three customers are >40% of
revenue. Any pause in hyperscaler capex would compress the multiple. China
export restrictions still cap a meaningful portion of demand.
"""


def _make_rm_state():
    # 리서치 매니저(Research Manager)에게 넘길 투자 토론 상태를 만든다.
    return {
        "company_of_interest": "NVDA",
        "investment_debate_state": {
            "history": DEBATE_HISTORY,
            "bull_history": "Bull Analyst: NVDA's data-center revenue grew 60% YoY...",
            "bear_history": "Bear Analyst: Concentration risk is real...",
            "current_response": "",
            "judge_decision": "",
            "count": 1,
        },
    }


def _make_trader_state(investment_plan: str):
    # 트레이더(Trader)에게 넘길 상태: 리서치 매니저의 투자 계획을 입력으로 받는다.
    return {
        "company_of_interest": "NVDA",
        "investment_plan": investment_plan,
    }


def _make_pm_state(investment_plan: str, trader_plan: str):
    # 포트폴리오 매니저(Portfolio Manager)에게 넘길 상태:
    # 리스크 토론 기록과 각종 리포트, 앞 단계 두 에이전트의 계획을 모두 포함한다.
    return {
        "company_of_interest": "NVDA",
        "past_context": "",
        "risk_debate_state": {
            "history": "Aggressive: lean in. Conservative: trim. Neutral: balanced sizing.",
            "aggressive_history": "Aggressive: ...",
            "conservative_history": "Conservative: ...",
            "neutral_history": "Neutral: ...",
            "judge_decision": "",
            "current_aggressive_response": "",
            "current_conservative_response": "",
            "current_neutral_response": "",
            "count": 1,
        },
        "market_report": "Market report.",
        "sentiment_report": "Sentiment report.",
        "news_report": "News report.",
        "fundamentals_report": "Fundamentals report.",
        "investment_plan": investment_plan,
        "trader_investment_plan": trader_plan,
    }


def _print_section(title: str, content: str) -> None:
    # 구분선(=)과 제목으로 감싼 섹션을 출력하는 보조 함수
    bar = "=" * 70
    print(f"\n{bar}\n{title}\n{bar}\n{content}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", choices=list(PROVIDER_DEFAULTS.keys()))
    parser.add_argument("--deep-model", default=None, help="Override deep_think_llm")
    parser.add_argument("--quick-model", default=None, help="Override quick_think_llm")
    args = parser.parse_args()

    default_model, _ = PROVIDER_DEFAULTS[args.provider]
    deep_model = args.deep_model or default_model
    quick_model = args.quick_model or default_model

    print(f"Provider: {args.provider}")
    print(f"Deep model:  {deep_model}")
    print(f"Quick model: {quick_model}")

    # 프레임워크의 팩토리(factory) 함수로 LLM 클라이언트들을 생성한다.
    deep_client = create_llm_client(provider=args.provider, model=deep_model)
    quick_client = create_llm_client(provider=args.provider, model=quick_model)
    deep_llm = deep_client.get_llm()
    quick_llm = quick_client.get_llm()

    # 1) 리서치 매니저(Research Manager)
    rm = create_research_manager(deep_llm)
    rm_result = rm(_make_rm_state())
    investment_plan = rm_result["investment_plan"]
    _print_section("[1] Research Manager — investment_plan", investment_plan)

    # 2) 트레이더(Trader) — 리서치 매니저의 계획을 입력으로 사용
    trader = create_trader(quick_llm)
    trader_result = trader(_make_trader_state(investment_plan))
    trader_plan = trader_result["trader_investment_plan"]
    _print_section("[2] Trader — trader_investment_plan", trader_plan)

    # 3) 포트폴리오 매니저(Portfolio Manager) — 앞의 두 결과를 모두 사용
    pm = create_portfolio_manager(deep_llm)
    pm_result = pm(_make_pm_state(investment_plan, trader_plan))
    final_decision = pm_result["final_trade_decision"]
    _print_section("[3] Portfolio Manager — final_trade_decision", final_decision)

    # 4) SignalProcessor는 LLM 호출 없이(제로 콜) 등급(rating)을 추출한다.
    sp = SignalProcessor()
    rating = sp.process_signal(final_decision)
    _print_section("[4] SignalProcessor → rating", rating)

    # 5) 가벼운 검증: 렌더링된 각 출력물에 기대하는 섹션 헤더(header)가
    #    들어 있는지 확인한다. 이 헤더들이 있어야 하위 소비자(메모리 로그,
    #    CLI 표시, 저장된 리포트)가 계속 정상 동작한다.
    checks = [
        ("Research Manager", investment_plan, ["**Recommendation**:"]),
        ("Trader",           trader_plan,     ["**Action**:", "FINAL TRANSACTION PROPOSAL:"]),
        ("Portfolio Manager", final_decision, ["**Rating**:", "**Executive Summary**:", "**Investment Thesis**:"]),
    ]
    print("\n" + "=" * 70 + "\nStructure checks\n" + "=" * 70)
    failures = 0
    # 각 에이전트 출력(text)에 필수 마커(marker) 문자열이 모두 포함됐는지 검사하고,
    # 빠진 개수를 failures에 누적한다. int(not ok)는 실패면 1, 성공이면 0이 된다.
    for name, text, required in checks:
        for marker in required:
            ok = marker in text
            print(f"  {'PASS' if ok else 'FAIL'}  {name}: contains {marker!r}")
            failures += int(not ok)

    print()
    if failures:
        print(f"Smoke FAILED: {failures} structure check(s) missing.")
        return 1
    print("Smoke PASSED: structured output → rendered markdown chain works for", args.provider)
    return 0


if __name__ == "__main__":
    sys.exit(main())
