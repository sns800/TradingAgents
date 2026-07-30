<p align="center">
  <img src="assets/TauricResearch.png" style="width: 60%; height: auto;">
</p>

<div align="center" style="line-height: 1;">
  <a href="https://arxiv.org/abs/2412.20138" target="_blank"><img alt="arXiv" src="https://img.shields.io/badge/arXiv-2412.20138-B31B1B?logo=arxiv"/></a>
  <a href="https://discord.com/invite/hk9PGKShPK" target="_blank"><img alt="Discord" src="https://img.shields.io/badge/Discord-TradingResearch-7289da?logo=discord&logoColor=white&color=7289da"/></a>
  <a href="https://x.com/TauricResearch" target="_blank"><img alt="X Follow" src="https://img.shields.io/badge/X-TauricResearch-white?logo=x&logoColor=white"/></a>
  <a href="https://github.com/TauricResearch/" target="_blank"><img alt="Community" src="https://img.shields.io/badge/GitHub_Community-TauricResearch-14C290?logo=discourse"/></a>
</div>
<br>
<div align="center">
  <a href="https://github.com/TauricResearch" target="_blank"><img alt="TradingAgents #1 Repository of the Day" src="https://trendshift.io/api/badge/repositories/16192" width="250" height="55"/></a>
</div>
<br>
<div align="center">
  <!-- Keep these links. Translations will automatically update with the README. -->
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=de">Deutsch</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=es">Español</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=fr">français</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=ja">日本語</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=ko">한국어</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=pt">Português</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=ru">Русский</a> | 
  <a href="https://www.readme-i18n.com/TauricResearch/TradingAgents?lang=zh">中文</a>
</div>

---

# TradingAgents: 멀티 에이전트 LLM 금융 트레이딩 프레임워크

## 소식 (News)
- [2026-07] **TradingAgents v0.3.1** 출시 — 정확성 및 안정성 수정 버전입니다: Alpha Vantage 미래 참조(look-ahead, 백테스트 시점에는 알 수 없었던 미래 데이터가 섞여 들어가는 문제) 필터링, 그래프 라우터 크래시 방지, 그래프 구조를 인식하는 체크포인트(checkpoint, 중간 저장 지점) 재개, 정상 동작하는 암호화폐 감성 데이터 소스, 설정 가능한 LLM 재시도 횟수(retry budget), Bedrock API 키 인증, Claude Sonnet 5 / Fable 5 지원. 전체 목록은 [CHANGELOG.md](CHANGELOG.md)를 참고하세요.
- [2026-06] **TradingAgents v0.3.0** 출시 — 검증된 데이터 접근 계약(data-access contract), 확장된 프로바이더 레지스트리(NVIDIA, Kimi, Groq, Mistral, Bedrock 및 모든 OpenAI 호환 엔드포인트), FRED·Polymarket 데이터 벤더, 최신 세대 모델 카탈로그, CI 게이트(자동 테스트 검증 절차)가 추가되었습니다.
- [2026-05] **TradingAgents v0.2.5** 출시 — 실제 데이터에 근거하는(grounded) 감성 분석가(Sentiment Analyst), GPT-5.5 등 모델 커버리지, Qwen/GLM/MiniMax 이중 리전 지원, API 키 자동 감지를 포함한 `TRADINGAGENTS_*` 환경변수 설정, 원격 Ollama 지원, 미국 외 시장용 알파 벤치마크, 티커 경로 탐색(path-traversal) 보안 강화가 포함되었습니다.
- [2026-04] **TradingAgents v0.2.4** 출시 — 구조화 출력(structured output) 에이전트(리서치 매니저, 트레이더, 포트폴리오 매니저), LangGraph 체크포인트 재개, 영속 의사결정 로그, DeepSeek/Qwen/GLM/Azure 프로바이더 지원, Docker, Windows UTF-8 인코딩 수정이 포함되었습니다.
- [2026-03] **TradingAgents v0.2.3** 출시 — 다국어 출력 지원, GPT-5.4 계열 모델, 통합 모델 카탈로그, 백테스팅 날짜 정합성, 프록시 지원이 포함되었습니다.
- [2026-03] **TradingAgents v0.2.2** 출시 — GPT-5.4/Gemini 3.1/Claude 4.6 모델 커버리지, 5단계 투자의견 체계, OpenAI Responses API, Anthropic effort 제어, 크로스 플랫폼 안정성이 포함되었습니다.
- [2026-02] **TradingAgents v0.2.0** 출시 — 멀티 프로바이더 LLM 지원(GPT-5.x, Gemini 3.x, Claude 4.x, Grok 4.x)과 개선된 시스템 아키텍처가 포함되었습니다.
- [2026-01] **Trading-R1** [기술 보고서](https://arxiv.org/abs/2509.11420) 공개 — [Terminal](https://github.com/TauricResearch/Trading-R1)도 곧 공개될 예정입니다.

<div align="center">

🚀 [TradingAgents](#tradingagents-프레임워크) | ⚡ [설치 및 CLI](#설치-및-cli) | 🎬 [데모](https://www.youtube.com/watch?v=90gr5lwjIho) | 📦 [패키지 사용법](#tradingagents-패키지) | 🤝 [기여하기](#기여하기) | 📄 [인용](#인용-citation)

</div>

> 🎉 **TradingAgents**가 공식 공개되었습니다! 이 연구에 대해 많은 문의를 받았으며, 커뮤니티의 뜨거운 관심에 감사드립니다.
>
> 그래서 프레임워크 전체를 오픈소스로 공개하기로 했습니다. 여러분과 함께 의미 있는 프로젝트를 만들어 가기를 기대합니다!

## TradingAgents 프레임워크

TradingAgents는 실제 트레이딩 회사의 운영 방식을 본뜬 멀티 에이전트 트레이딩 프레임워크입니다. 펀더멘털 분석가(Fundamentals Analyst), 감성 전문가, 기술적 분석가부터 트레이더, 리스크 관리 팀까지 역할별로 특화된 LLM(대규모 언어 모델) 기반 에이전트들을 배치하여, 플랫폼이 협업 방식으로 시장 상황을 평가하고 트레이딩 의사결정에 근거를 제공합니다. 나아가 이 에이전트들은 역동적인 토론을 통해 최적의 전략을 찾아냅니다.

<p align="center">
  <img src="assets/schema.png" style="width: 100%; height: auto;">
</p>

> TradingAgents 프레임워크는 연구 목적으로 설계되었습니다. 트레이딩 성과는 선택한 기반 언어 모델, 모델 온도(temperature, 출력의 무작위성을 조절하는 값), 거래 기간, 데이터 품질, 기타 비결정적 요인 등 여러 요소에 따라 달라질 수 있습니다. [본 프레임워크는 금융, 투자, 트레이딩 조언을 목적으로 하지 않습니다.](https://tauric.ai/disclaimer/)

우리 프레임워크는 복잡한 트레이딩 작업을 전문화된 역할들로 분해합니다.

### 분석가 팀 (Analyst Team)
- 펀더멘털 분석가(Fundamentals Analyst): 기업의 재무제표와 성과 지표를 평가하여 내재 가치(intrinsic value)와 잠재적 위험 신호를 찾아냅니다.
- 감성 분석가(Sentiment Analyst): 뉴스 헤드라인, StockTwits(주식 토론 SNS), Reddit 게시글을 하나의 감성 지표로 종합하여 단기 시장 심리를 가늠합니다.
- 뉴스 분석가(News Analyst): 글로벌 뉴스와 거시경제 지표를 모니터링하며, 각종 사건이 시장 상황에 미치는 영향을 해석합니다.
- 기술적 분석가(Technical Analyst): MACD, RSI 같은 기술적 지표(과거 가격·거래량 데이터로 매매 신호를 찾는 보조지표)를 활용해 거래 패턴을 포착하고 가격 움직임을 예측합니다.

<p align="center">
  <img src="assets/analyst.png" width="100%" style="display: inline-block; margin: 0 2%;">
</p>

### 리서처 팀 (Researcher Team)
- 강세론(bullish) 리서처와 약세론(bearish) 리서처로 구성되어, 분석가 팀이 제공한 인사이트를 비판적으로 검토합니다. 구조화된 토론을 통해 잠재적 수익과 내재된 리스크의 균형을 맞춥니다.

<p align="center">
  <img src="assets/researcher.png" width="70%" style="display: inline-block; margin: 0 2%;">
</p>

### 트레이더 에이전트 (Trader Agent)
- 분석가와 리서처의 보고서를 종합하여 정보에 기반한 트레이딩 의사결정을 내리고, 매매 시점과 규모를 결정합니다.

<p align="center">
  <img src="assets/trader.png" width="70%" style="display: inline-block; margin: 0 2%;">
</p>

### 리스크 관리 및 포트폴리오 매니저 (Risk Management and Portfolio Manager)
- 시장 변동성, 유동성 및 기타 리스크 요인을 평가하여 포트폴리오 리스크를 지속적으로 점검합니다. 리스크 관리 팀은 트레이딩 전략을 평가·조정하고, 최종 결정을 위해 포트폴리오 매니저(Portfolio Manager)에게 평가 보고서를 제공합니다.
- 포트폴리오 매니저는 거래 제안을 승인하거나 반려합니다. 승인되면 주문이 모의 거래소(simulated exchange)로 전송되어 체결됩니다.

<p align="center">
  <img src="assets/risk.png" width="70%" style="display: inline-block; margin: 0 2%;">
</p>

## 설치 및 CLI

### 설치

TradingAgents를 클론(clone)합니다:
```bash
git clone https://github.com/TauricResearch/TradingAgents.git
cd TradingAgents
```

선호하는 환경 관리자로 가상 환경(virtual environment)을 생성합니다:
```bash
conda create -n tradingagents python=3.12
conda activate tradingagents
```

패키지와 의존성을 설치합니다:
```bash
pip install .
```

### Docker

또는 Docker로 실행할 수 있습니다:
```bash
cp .env.example .env  # add your API keys
docker compose run --rm tradingagents
```

Ollama를 통한 로컬 모델 사용 시:
```bash
docker compose --profile ollama run --rm tradingagents-ollama
```

### 필수 API

TradingAgents는 여러 LLM 프로바이더(제공업체)를 지원합니다. 사용할 프로바이더의 API 키를 설정하세요:

```bash
export OPENAI_API_KEY=...          # OpenAI (GPT)
export GOOGLE_API_KEY=...          # Google (Gemini)
export ANTHROPIC_API_KEY=...       # Anthropic (Claude)
export XAI_API_KEY=...             # xAI (Grok)
export DEEPSEEK_API_KEY=...        # DeepSeek
export DASHSCOPE_API_KEY=...       # Qwen — International (dashscope-intl.aliyuncs.com)
export DASHSCOPE_CN_API_KEY=...    # Qwen — China (dashscope.aliyuncs.com)
export ZHIPU_API_KEY=...           # GLM via Z.AI (international)
export ZHIPU_CN_API_KEY=...        # GLM via BigModel (China, open.bigmodel.cn)
export MINIMAX_API_KEY=...         # MiniMax — Global (api.minimax.io)
export MINIMAX_CN_API_KEY=...      # MiniMax — China (api.minimaxi.com)
export OPENROUTER_API_KEY=...      # OpenRouter
export ALPHA_VANTAGE_API_KEY=...   # Alpha Vantage
```

Azure OpenAI를 사용하려면 `.env.enterprise.example`을 `.env.enterprise`로 복사한 뒤 인증 정보를 입력하세요.

AWS Bedrock을 사용하려면 `pip install ".[bedrock]"`으로 추가 의존성을 설치하고, `llm_provider: "bedrock"`을 설정한 뒤 AWS 인증 정보(환경변수, `~/.aws/credentials`, 또는 IAM 역할)와 `AWS_DEFAULT_REGION`을 구성하고, Bedrock 모델 ID(예: `us.anthropic.claude-opus-4-8-v1:0`)를 사용하세요.

로컬 모델을 사용하려면 `llm_provider: "ollama"`로 Ollama를 설정하세요. 기본 엔드포인트는 `http://localhost:11434/v1`이며, 원격 `ollama-serve`를 가리키려면 `OLLAMA_BASE_URL`을 설정하세요. `ollama pull <name>`으로 모델을 내려받고, 기본 목록에 없는 모델은 CLI에서 "Custom model ID"를 선택하면 됩니다.

그 밖의 OpenAI 호환 서버(vLLM, LM Studio, llama.cpp, 또는 커스텀 릴레이)를 사용하려면 `llm_provider: "openai_compatible"`을 지정하고 `backend_url`(또는 `TRADINGAGENTS_LLM_BACKEND_URL`)로 엔드포인트를 설정하세요. 예: vLLM은 `http://localhost:8000/v1`, LM Studio는 `http://localhost:1234/v1`. 모델은 해당 서버가 제공하는 것을 그대로 사용합니다. 로컬 서버에는 키가 필요 없으며, 엔드포인트가 키를 요구하는 경우 `OPENAI_COMPATIBLE_API_KEY`를 설정하세요.

또는 `.env.example`을 `.env`로 복사한 뒤 키를 입력해도 됩니다:
```bash
cp .env.example .env
```

### CLI 사용법

대화형 CLI(명령줄 인터페이스)를 실행합니다:
```bash
tradingagents          # installed command
python -m cli.main     # alternative: run directly from source
```
원하는 티커(ticker, 종목 코드), 분석 날짜, LLM 프로바이더, 리서치 깊이 등을 선택할 수 있는 화면이 나타납니다.

### 시장 및 티커

TradingAgents는 Yahoo Finance가 다루는 모든 시장에서 동작하며, 거래소 접미사가 붙은 티커를 사용합니다. 기업 식별 정보와 알파 벤치마크(alpha benchmark, 초과수익 비교 기준 지수)는 시장별로 자동으로 결정됩니다.

- 미국: `AAPL`, `SPY`
- 홍콩: `0700.HK` · 도쿄: `7203.T` · 런던: `AZN.L`
- 인도: `RELIANCE.NS`, `.BO` · 캐나다: `.TO` · 호주: `.AX`
- 중국 A주: 상하이 `.SS`, 선전 `.SZ` (예: 구이저우 마오타이는 `600519.SS`)
- 암호화폐: `BTC-USD`, `ETH-USD`

<p align="center">
  <img src="assets/cli/cli_init.png" width="100%" style="display: inline-block; margin: 0 2%;">
</p>

결과가 로드되는 대로 보여주는 인터페이스가 나타나며, 에이전트가 실행되는 동안 진행 상황을 실시간으로 확인할 수 있습니다.

<p align="center">
  <img src="assets/cli/cli_news.png" width="100%" style="display: inline-block; margin: 0 2%;">
</p>

<p align="center">
  <img src="assets/cli/cli_transaction.png" width="100%" style="display: inline-block; margin: 0 2%;">
</p>

## TradingAgents 패키지

### 구현 세부사항

TradingAgents는 유연성과 모듈성을 확보하기 위해 LangGraph(LLM 에이전트 워크플로 오케스트레이션 라이브러리)로 구축되었습니다. 프레임워크는 여러 LLM 프로바이더를 지원합니다: OpenAI, Google, Anthropic, xAI, DeepSeek, Qwen(Alibaba DashScope, 국제·중국 엔드포인트), GLM(Zhipu), MiniMax(글로벌 + 중국), OpenRouter, 로컬 모델용 Ollama, 엔터프라이즈용 Azure OpenAI.

### Python 사용법

코드 안에서 TradingAgents를 사용하려면 `tradingagents` 모듈을 임포트하고 `TradingAgentsGraph()` 객체를 초기화하면 됩니다. `.propagate()` 함수가 의사결정 결과를 반환합니다. `main.py`를 실행해도 되고, 아래의 간단한 예제를 참고하세요:

```python
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

ta = TradingAgentsGraph(debug=True, config=DEFAULT_CONFIG.copy())

# forward propagate
_, decision = ta.propagate("NVDA", "2026-01-15")
print(decision)
```

기본 설정을 조정하여 원하는 LLM, 토론 라운드 수 등을 직접 지정할 수도 있습니다.

```python
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

config = DEFAULT_CONFIG.copy()
config["llm_provider"] = "openai"        # e.g. openai, google, anthropic, deepseek, groq, ollama; openai_compatible covers any OpenAI-compatible endpoint (vLLM, LM Studio, llama.cpp, ...)
config["deep_think_llm"] = "gpt-5.5"     # Model for complex reasoning
config["quick_think_llm"] = "gpt-5.4-mini" # Model for quick tasks
config["max_debate_rounds"] = 2

ta = TradingAgentsGraph(debug=True, config=config)
_, decision = ta.propagate("NVDA", "2026-01-15")
print(decision)
```

전체 설정 옵션은 `tradingagents/default_config.py`를 참고하세요.

## 영속성과 복구 (Persistence and Recovery)

TradingAgents는 두 가지 상태를 실행 간에 영속적으로 유지합니다.

### 의사결정 로그 (Decision log)

의사결정 로그는 항상 켜져 있습니다. 실행이 완료될 때마다 그 의사결정이 `~/.tradingagents/memory/trading_memory.md`에 추가됩니다. 같은 티커로 다음 실행을 하면 TradingAgents가 실현 수익률(절대 수익률과 SPY 대비 알파)을 가져오고, 한 문단짜리 회고(reflection)를 생성한 뒤, 가장 최근의 동일 티커 의사결정들과 다른 티커에서 얻은 최근 교훈을 포트폴리오 매니저 프롬프트에 주입합니다. 이렇게 매 분석은 무엇이 통했고 무엇이 통하지 않았는지를 이어받습니다.

로그 경로는 `TRADINGAGENTS_MEMORY_LOG_PATH`로 변경할 수 있습니다.

### 체크포인트 재개 (Checkpoint resume)

체크포인트 재개는 `--checkpoint` 옵션으로 선택 활성화합니다. 활성화하면 LangGraph가 각 노드 실행 후 상태를 저장하므로, 실행이 중단되거나 크래시되어도 처음부터 다시 시작하지 않고 마지막으로 성공한 단계부터 재개합니다. 재개 실행 시 로그에 `Resuming from step N for <TICKER> on <date>`가, 새 실행 시 `Starting fresh`가 표시됩니다. 체크포인트는 실행이 성공적으로 완료되면 자동으로 삭제됩니다.

티커별 SQLite 데이터베이스는 `~/.tradingagents/cache/checkpoints/<TICKER>.db`에 저장됩니다(기본 경로는 `TRADINGAGENTS_CACHE_DIR`로 변경 가능). 실행 전에 전부 초기화하려면 `--clear-checkpoints`를 사용하세요.

```bash
tradingagents analyze --checkpoint           # enable for this run
tradingagents analyze --clear-checkpoints    # reset before running
```

```python
config = DEFAULT_CONFIG.copy()
config["checkpoint_enabled"] = True
ta = TradingAgentsGraph(config=config)
_, decision = ta.propagate("NVDA", "2026-01-15")
```

## 재현성 (Reproducibility)

TradingAgents는 LLM 기반이므로 같은 티커와 날짜로 두 번 실행해도 결과가 다를 수 있습니다. 이는 언어 모델 위에 구축된 연구 도구에서 예상되는 현상이지 결함이 아닙니다. 변동은 몇 가지 서로 다른 원인에서 비롯되므로 구분해서 이해하는 것이 좋습니다.

언어 모델 샘플링은 비결정적입니다. 온도(temperature)를 고정하더라도 프로바이더는 호출 간 바이트 단위로 동일한 출력을 보장하지 않으며, 추론(reasoning) 모델(기본값인 GPT-5.x 계열 및 모든 thinking 모드 모델)은 내부 추론 과정 자체가 샘플링되기 때문에 변동이 가장 큽니다.

실시간 데이터는 계속 변합니다. 뉴스, StockTwits, Reddit은 시간이 지나면 다른 내용을 반환하므로, 같은 과거 거래 날짜라도 오늘 실행과 지난주 실행은 서로 다른 입력을 보게 됩니다. 분석 날짜를 고정하면 가격과 지표 구간은 고정되지만, 소셜·뉴스 소스는 여전히 "지금"을 반영합니다.

변동을 줄이려면 샘플링 온도를 낮출 수 있습니다. 설정에서 `temperature`를 지정하거나 `.env`에서 `TRADINGAGENTS_TEMPERATURE`를 설정하세요. 온도를 존중하는 모델일수록 값이 낮으면 더 반복 가능해집니다. 다만 현재 큐레이션된 모델은 추론 우선(reasoning-first)이라 온도를 대부분 무시하므로, 더 엄격한 재현성이 필요하면 비추론(non-reasoning) 모델을 Custom model ID 옵션으로 명시적으로 지정하세요.

```python
config = DEFAULT_CONFIG.copy()
config["llm_provider"] = "openai"
config["temperature"] = 0.0
# Reasoning models ignore temperature. For tighter reproducibility, set a
# non-reasoning deep/quick model explicitly (e.g. via the Custom model ID option).
```

더 이상 변하지 않는 것: 분석 대상 기업의 식별 정보는 어떤 에이전트도 실행되기 전에 티커로부터 결정론적으로 확정되며, 마켓 분석가는 정확한 가격·지표 주장을 검증된 데이터 스냅샷에 근거해 작성합니다. 실행마다 "다른 회사"가 분석되거나 가격 수치가 지어내지던 과거의 문제는 이 두 메커니즘으로 해결되었습니다.

백테스트(backtest, 과거 데이터로 전략을 검증하는 것) 결과가 공개된 수치와 일치한다는 보장은 없습니다. 수익률은 모델, 온도, 날짜 범위, 데이터 품질, 그리고 위에서 설명한 샘플링에 따라 달라집니다. 이 프레임워크는 고정적이고 재현 가능한 수익률을 내는 전략이 아니라, 멀티 에이전트 분석을 연구하기 위한 연구용 발판(scaffold)으로 다루어 주세요.

## 기여하기

기여를 환영합니다: 버그 수정, 문서화, 기능 아이디어 모두 좋습니다. 과거 기여자는 릴리스별로 [`CHANGELOG.md`](CHANGELOG.md)에 기록됩니다.

## 인용 (Citation)

*TradingAgents*가 도움이 되었다면 아래와 같이 저희 연구를 인용해 주세요 :)

```
@misc{xiao2025tradingagentsmultiagentsllmfinancial,
      title={TradingAgents: Multi-Agents LLM Financial Trading Framework}, 
      author={Yijia Xiao and Edward Sun and Di Luo and Wei Wang},
      year={2025},
      eprint={2412.20138},
      archivePrefix={arXiv},
      primaryClass={q-fin.TR},
      url={https://arxiv.org/abs/2412.20138}, 
}
```
