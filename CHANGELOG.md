# 변경 이력 (Changelog)

TradingAgents의 주요 변경 사항을 모두 이 문서에 기록합니다.

문서 형식은 [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)를 따르며,
이 프로젝트는 [유의적 버전(Semantic Versioning)](https://semver.org/spec/v2.0.0.html)을 준수합니다.
0.x 버전대 내의 호환성 파괴 변경(breaking change)은 별도로 명시합니다.

## [0.3.1] — 2026-07-05

정확성 및 안정성 패치: 데이터 미래 참조(look-ahead), 그래프 라우터 크래시 방지,
체크포인트 식별, 암호화폐 감성 데이터 소스, 설정 가능한 복원력이 포함되었습니다.

### 수정 (Fixed)

- **Alpha Vantage 미래 참조 필터가 이제 실제로 실행됩니다.** 펀더멘털 응답이
  JSON 문자열이어서 딕셔너리만 처리하던 가드가 필터링을 건너뛰었고, 그 결과
  미래 날짜의 보고서가 과거 시점 실행에 유입되었습니다. 필터링 전에 파싱하도록
  수정했습니다. (#1115, @zachthebird)
- **뉴스 분석가 프롬프트가 실제 도구와 일치합니다.** 프롬프트는
  `get_news(query, ...)`라고 안내했지만 도구는 티커를 받습니다. 자유 텍스트
  질의를 지어내서 호출하는 문제(hallucination)를 막도록 맞췄습니다. (#1116, @shcheuk)
- **공유 토론/리스크 라우터가 실행 중에 크래시하지 않습니다.** 두 라우터 모두
  개별 엣지(edge)에 매핑된 것보다 많은 대상을 반환할 수 있었는데, 이제 모든
  엣지가 전체 경로 맵을 공유하므로 프롬프트/다국어/리팩터링 변화로 예상 밖
  경로에 빠져도 라우팅이 유지됩니다. (#1088, @Fr3ya, @sa7an7, @Sushanth012)
- **체크포인트 재개가 그래프 구조를 존중합니다.** 스레드 ID에 선택한 분석가,
  토론/리스크 깊이, 자산 모드가 반영되므로, 다른 설정으로 재개해도 잘못된
  그래프를 이어서 실행하지 않습니다. (#1089, @bossjoker1, @Ghraven)
- **암호화폐 감성 데이터 소스가 정상 조회됩니다.** StockTwits는 암호화폐를
  `<BASE>.X` 형식으로 표기하고(Yahoo 형식인 `BTC-USD`는 404 발생),
  Reddit은 기본 심볼로 검색해야 매칭됩니다. 소셜 경로가 이제 두 경우 모두
  암호화폐를 올바르게 매핑합니다. (#1113, @suremadoreai)

### 추가 (Added)

- **설정 가능한 LLM 재시도 횟수(retry budget).** `llm_max_retries` /
  `TRADINGAGENTS_LLM_MAX_RETRIES` 값이 모든 프로바이더에 전달되므로, 일시적인
  429(요청 과다) 오류가 몰려도 실행이 중단되지 않습니다. (#1091, @yanggaome)
- **Bedrock API 키 인증.** `AWS_BEARER_TOKEN_BEDROCK`으로 AWS 액세스 키 없이
  Amazon Bedrock에 인증할 수 있으며, 환경에 설정된 `AWS_PROFILE`보다
  우선합니다. (#1103, @praxstack)
- **최신 Claude 모델.** Claude Sonnet 5(`claude-sonnet-5`)와
  Fable 5(`claude-fable-5`)를 추가했으며, effort 제어가 Claude 5 라인까지
  확장되었습니다.

## [0.3.0] — 2026-06-22

안정화 및 확장성 릴리스: CI 게이트, 통합된 검증 데이터 접근 계약(data-access
contract), 프로바이더·데이터 벤더 레지스트리, 그리고 설정 우선순위·모델
카탈로그·데이터 복원력·구조화 출력을 강화한 유지보수 작업이 포함되었습니다.

### 추가 (Added)

- **CI 게이트.** GitHub Actions가 Python 3.10-3.13 전반에서 pytest 스위트,
  엄격 모드 `ruff`(코드 검사 도구), 그리고 패키지와 CLI를 임포트하는
  클린 설치 스모크 테스트를 실행하여 선언되지 않은 의존성을 잡아냅니다.
  (#994, #197)
- **프로바이더 레지스트리.** OpenAI 호환 프로바이더가 단일 스펙으로 등록되며,
  범용 `openai_compatible` 엔드포인트가 vLLM, LM Studio, 릴레이를 지원합니다.
  NVIDIA NIM, Kimi, Groq, Mistral 및 네이티브 Amazon Bedrock 클라이언트가
  추가되었습니다.
- **거시경제·예측시장 벤더.** FRED 거시경제 지표와 Polymarket(예측시장 플랫폼)
  이벤트 확률이 추가되어 뉴스 및 거시 분석가에게 제공됩니다.
- **프로그래밍 방식 보고서 출력.** `TradingAgentsGraph.save_reports()`가 CLI가
  만드는 것과 동일한 보고서 트리를 기록하므로, 헤드리스(화면 없는) 실행이나
  API 실행에서도 보고서를 저장할 수 있습니다. (#1037)
- **환경변수로 설정 가능한 추론 깊이** — `TRADINGAGENTS_OPENAI_REASONING_EFFORT`,
  `TRADINGAGENTS_GOOGLE_THINKING_LEVEL`, `TRADINGAGENTS_ANTHROPIC_EFFORT`가
  추가되었으며, 각각 해당 옵션을 지원하는 모델에만 적용됩니다.

### 변경 (Changed)

- **검증된 데이터 접근 계약.** 모든 벤더 경로(식별, 수익률, CLI, 뉴스)에서 심볼
  정규화를 수행하고, 설정된 벤더 목록이 정확한 조회 체인이 되어 선택하지 않은
  벤더로 조용히 폴백(fallback)하지 않으며, 타입화된 `VendorError` 분류 체계,
  미래 참조가 없는 뉴스 기간 처리, 오래된 OHLCV(시가·고가·저가·종가·거래량)
  데이터 거부, yfinance 날짜 범위의 양끝 포함 처리가 적용되었습니다.
- **설정 우선순위.** 명시적인 `TRADINGAGENTS_*` 값이나 CLI 플래그가 이제 토론·
  리스크 라운드 수, `--checkpoint / --no-checkpoint`, Docker 프로바이더
  프로필에 대해 대화형 기본값보다 우선하며, 잘못된 불리언 환경변수 값은
  명확한 오류를 냅니다. (#975, #976, #977)
- **최신 세대 모델 카탈로그.** 프로바이더별 모델 목록을 갱신했으며,
  `gpt-4.1`, Claude Sonnet 4.5, Gemini 2.5 라인을 정리했습니다.
- **선택적 벤더는 실행을 중단시키지 않고 우아하게 실패합니다**: 거시경제나
  예측시장 조회가 실패하면 데이터 없음 표시 값(sentinel)을 반환합니다.
- **분석가 프롬프트가 현재 날짜로 시작합니다.** 도구 호출의 날짜 범위가 모델의
  학습 데이터 기준일이 아니라 실행 날짜에 고정되도록 했습니다. (#836)

### 수정 (Fixed)

- **종목 식별.** 티커에서 기업으로의 결정론적 매핑으로 잘못된 기업을 지어내는
  환각(hallucination)을 방지하고, 검증된 시장 데이터 스냅샷이 가격·지표 주장의
  근거가 됩니다. (#814, #830)
- **소셜·시장 데이터 소스.** Reddit은 RSS 우선에 429 백오프(재시도 전 대기)를
  적용하고, StockTwits 전송 계층을 강화했으며, Alpha Vantage에 타임아웃과
  API 키 오류-요청 제한 구분 처리를 추가했습니다.
- **구조화 출력.** 로컬 OpenAI 호환 서버가 객체 형태의 `tool_choice`를 더 이상
  거부하지 않고, 파싱된 결과를 반환하지 않는 thinking 모델은 자유 텍스트로
  폴백하며, 선택적 가격 필드의 null성 문자열은 `None`으로 변환됩니다.
  (#1038, #1051, #1057)

### 제거 (Removed)

- 아무 동작도 하지 않던 `analyst_concurrency_limit` 설정 항목. 분석가 병렬
  실행은 이후 릴리스에서 지원할 예정입니다. (#979)
- 사용되지 않는 채로 커밋되어 있던 `uv.lock`. (#1030)

### 기여자 (Contributors)

코드, 설계, 리포트로 이번 릴리스에 기여해 주신 모든 분께 감사드립니다:

[@CadeYu](https://github.com/CadeYu), [@Zavianx](https://github.com/Zavianx), [@weijianz-opc](https://github.com/weijianz-opc), [@naltun](https://github.com/naltun), [@brahmasky](https://github.com/brahmasky), [@nik2208](https://github.com/nik2208), [@thieucong98](https://github.com/thieucong98), [@Derekko-web](https://github.com/Derekko-web), [@LukiPrince](https://github.com/LukiPrince), [@Eddieargenal](https://github.com/Eddieargenal), [@Ghraven](https://github.com/Ghraven), [@ms32035](https://github.com/ms32035), [@yting27](https://github.com/yting27), [@nyxst4ck](https://github.com/nyxst4ck), [@KenCheung-AIxFinance](https://github.com/KenCheung-AIxFinance), [@yangyusheng2n](https://github.com/yangyusheng2n), [@fareloj](https://github.com/fareloj), [@haosenwang1018](https://github.com/haosenwang1018), [@octo-patch](https://github.com/octo-patch), [@seifenk](https://github.com/seifenk), [@CaoYuhaoCarl](https://github.com/CaoYuhaoCarl), [@mihailnica10](https://github.com/mihailnica10), [@Dado-hash](https://github.com/Dado-hash), [@Handsomemikezzz](https://github.com/Handsomemikezzz), [@ydhawesome](https://github.com/ydhawesome), [@macd2](https://github.com/macd2), [@AyushKar2005](https://github.com/AyushKar2005), [@wildhuman](https://github.com/wildhuman), [@robert23kim](https://github.com/robert23kim), [@bngness](https://github.com/bngness), [@tedix-rodrigo](https://github.com/tedix-rodrigo), [@malaccan](https://github.com/malaccan), [@rfalken78](https://github.com/rfalken78), [@dengli1971-droid](https://github.com/dengli1971-droid), [@proofconcept39](https://github.com/proofconcept39), [@prasta1](https://github.com/prasta1), [@liximin](https://github.com/liximin), [@jeffhuen](https://github.com/jeffhuen), [@mazar](https://github.com/mazar), [@soyangelromero](https://github.com/soyangelromero), [@CNQQC](https://github.com/CNQQC), [@dovetaill](https://github.com/dovetaill), [@fperdigon](https://github.com/fperdigon), [@gyx09212214-prog](https://github.com/gyx09212214-prog), [@RSXLX](https://github.com/RSXLX).

## [0.2.5] — 2026-05-11

### 추가 (Added)

- **실제 데이터에 근거하는 감성 분석가(Sentiment Analyst).** 이름이 변경된
  `sentiment_analyst`는 이제 보고서를 생성하기 전에 실제 Yahoo News,
  StockTwits, Reddit 데이터를 읽습니다. 프롬프트 압박에 밀려 소셜 게시글을
  지어낼 수 있었던 기존 흐름을 대체합니다. (#557, #607)
- **MiniMax 프로바이더** — M2.x 전체 카탈로그(M2.7 / M2.5 / M2.1 / M2와
  highspeed 변형, 204K 컨텍스트) 지원. 이중 리전: 글로벌(`MINIMAX_API_KEY`)과
  중국(`MINIMAX_CN_API_KEY`).
- **Qwen·GLM 이중 리전 지원** — 리전별로 별도의 키를 사용합니다. 국제
  (`DASHSCOPE_API_KEY`, `ZHIPU_API_KEY`)와 중국(`DASHSCOPE_CN_API_KEY`,
  `ZHIPU_CN_API_KEY`)이며, 보조 리전 선택 프롬프트로 선택할 수 있습니다. (#758)
- **`DEFAULT_CONFIG`의 `TRADINGAGENTS_*` 환경변수 설정 지원.**
  `llm_provider`, deep/quick 모델 ID, `backend_url`, `output_language`,
  토론 라운드 수, 체크포인트 플래그, 벤치마크 티커를 `.env`에서 재정의할 수
  있으며, 타입을 인식해 변환합니다(문자열 / 정수 / 불리언). (#602)
- **CLI의 대화형 API 키 감지.** 선택한 프로바이더의 키가 없으면 CLI가 키를
  입력받아 `.env`에 저장하므로, 재시작 없이 분석 실행이 이어집니다.
- **원격 Ollama 지원.** `OLLAMA_BASE_URL`로 CLI와 프로그래밍 방식 클라이언트가
  원격 `ollama-serve`를 가리킬 수 있습니다. CLI는 최종 결정된 엔드포인트를
  표시하고 흔한 입력 오류에 대해 경고합니다. `ollama pull`로 내려받은 모델을
  위한 `"Custom model ID"` 옵션이 추가되었습니다. (#648, #768)
- **뉴스 수집 파라미터 설정 지원** — `DEFAULT_CONFIG`에서 티커별 기사 수 제한,
  거시경제 헤드라인 수 제한, 조회 기간(lookback window), 거시경제 검색 질의를
  설정할 수 있습니다. (#606, #683)
- **미국 외 티커를 위한 알파 벤치마크 설정 지원.** 하드코딩된 SPY를 지역
  지수로 대체합니다: `.NS`(^NSEI), `.T`(^N225), `.HK`(^HSI), `.L`(^FTSE),
  `.TO`(^GSPTSE), `.AX`(^AXJO), `.BO`(^BSESN). `benchmark_ticker`로 명시적
  재정의도 가능합니다. 비달러 상장 종목의 알파가 환율 변동에 좌우되는 문제를
  없앱니다. (#628, #684)
- **다국어 출력이 사용자에게 노출되는 모든 에이전트를 포함합니다** — 리서처,
  리스크 토론자, 리서치 매니저, 트레이더까지 적용되어, 보고서 일부만
  번역되던 문제가 해결되었습니다. (#575)
- **모델 카탈로그 갱신.** OpenAI GPT-5.5 프런티어, Anthropic Claude Opus 4.7,
  Gemini 3.1 Flash-Lite GA, xAI Grok 4.20, Qwen 3.6 라인. 버전이 명시된 ID만
  사용하며, 자동으로 바뀌는 별칭(alias)은 `"Custom model ID"` 옵션으로
  옮겼습니다.

### 변경 (Changed)

- **감성 분석가(Sentiment Analyst)** 명칭이 CLI 드롭다운, 상태 패널, 최종
  보고서에서 일관되게 표시됩니다(이전에는 백엔드만 이름이 바뀌고 CLI는 여전히
  "Social Analyst"로 표시). 저장된 설정과의 하위 호환을 위해
  `AnalystType.SOCIAL = "social"` 값은 유지됩니다.

### 수정 (Fixed)

- **DeepSeek V4 / reasoner와 MiniMax M2.x에서 구조화 출력이 동작합니다.**
  이들 프로바이더는 자사 도구 호출 문서에 따라 `tool_choice`를 거부하므로,
  바인딩 흐름이 기능 지원 표(capability table)를 통해 자동으로 이를
  건너뜁니다.
- **`pip install .`로 설치한 경우에도** 콘솔 스크립트로 CLI를 실행할 때
  프로젝트 `.env`를 읽어옵니다. (#747)
- **보고서가 끝까지 저장됩니다** — 이전에는 스트리밍 청크가
  `complete_report.md`에서 누락되었습니다. (#719, #736)
- **티커 입력이 거래소 접미사를 보존합니다** (`.SH`, `.SZ`, `.SS`, `.HK`,
  `.T` 등) — A주, 홍콩, 도쿄 등 미국 외 시장 흐름에 적용됩니다. (#770)
- **Docker 권한 오류**가 더 이상 최초 실행 시 `~/.tradingagents/` 쓰기를
  막지 않습니다. (#519, #627, #672, #771)
- **하위 딕셔너리 변경 시 설정 상태가 실행 간에 누출되지 않습니다.**
  `set_config` 부분 업데이트가 형제 기본값을 보존합니다. (#788)
- **`max_recur_limit` 설정이 실제로 적용됩니다** — 이전에는 값을 읽기만 하고
  전파기(propagator)에 전달하지 않았습니다. (#764)
- **API 키 누락 오류**가 설정해야 할 정확한 환경변수 이름을 알려줍니다. (#680)
- **더 조용한 시작** — langgraph-checkpoint에서 발생하는 시끄러운 업스트림
  `LangChainPendingDeprecationWarning`을 억제했습니다. 해당 패키지가 수정을
  배포하면 제거할 예정입니다.

### 보안 (Security)

- **티커 경로 탐색(path-traversal) 검증**을 모든 파일시스템 경로 지점(캐시,
  체크포인트 데이터베이스, 결과)에 적용하여, 악의적인 티커가 지정된 디렉터리를
  벗어날 수 없게 했습니다. (#618)

## [0.2.4] — 2026-04-25

### 추가 (Added)

- **구조화 출력 의사결정 에이전트.** 리서치 매니저, 트레이더, 포트폴리오
  매니저가 이제 주요 호출에서 `llm.with_structured_output(Schema)`를 사용하고
  타입이 지정된 Pydantic 인스턴스를 반환합니다. 각 프로바이더의 네이티브
  구조화 출력 방식을 사용합니다(OpenAI / xAI는 `json_schema`, Gemini는
  `response_schema`, Anthropic은 도구 사용(tool-use), OpenAI 호환
  프로바이더는 함수 호출(function-calling)). 렌더링 헬퍼가 기존 마크다운
  형태를 보존하므로 메모리 로그, CLI 표시, 저장된 보고서는 변경 없이 그대로
  동작합니다. (#434)
- **LangGraph 체크포인트 재개** — `--checkpoint`로 선택 활성화합니다. 각 노드
  실행 후 상태가 저장되어 크래시되거나 중단된 실행이 마지막 성공 단계부터
  재개됩니다. 티커별 SQLite 데이터베이스는
  `~/.tradingagents/cache/checkpoints/` 아래에 있으며,
  `--clear-checkpoints`로 초기화합니다. (#594)
- **영속 의사결정 로그** — 에이전트별 BM25 메모리를 대체합니다. `propagate()`
  종료 시 의사결정이 자동으로 저장되며, 같은 티커의 다음 실행에서 이전 대기
  항목이 실현 수익률, SPY 대비 알파, 한 문단 회고와 함께 확정됩니다. 경로는
  `TRADINGAGENTS_MEMORY_LOG_PATH`로 변경합니다. 선택적
  `memory_log_max_entries` 설정으로 확정된 항목 수를 제한할 수 있으며, 대기
  중인 항목은 절대 정리되지 않습니다. (#578, #563, #564, #579)
- **DeepSeek, Qwen(Alibaba DashScope), GLM(Zhipu), Azure OpenAI** 프로바이더
  추가 및 OpenRouter 동적 모델 선택.
- **Docker 지원** — 개발 이미지와 런타임 이미지를 분리한 멀티 스테이지 빌드.
- **`scripts/smoke_structured_output.py`** — 세 구조화 출력 에이전트를 임의의
  프로바이더에 대해 실행해 보는 진단 스크립트로, 기여자가 명령 하나로 자신의
  환경을 검증할 수 있습니다.
- **5단계 투자의견 체계** (Buy / Overweight / Hold / Underweight / Sell,
  매수 / 비중확대 / 보유 / 비중축소 / 매도) — 리서치 매니저, 포트폴리오
  매니저, 시그널 프로세서, 메모리 로그가 일관되게 사용합니다. 트레이더는 거래
  방향이 본질적으로 3가지이므로 3단계(Buy / Hold / Sell)를 유지합니다.
- **Pytest 픽스처** — LLM 클라이언트 지연 임포트와 자리표시자 API 키를 통해
  자격 증명 없이도 테스트 스위트가 깨끗하게 실행됩니다. (#588)

### 변경 (Changed)

- **`backend_url` 기본값이 OpenAI URL 대신 `None`이 되었습니다.** 각
  프로바이더 클라이언트가 자체 기본값으로 폴백합니다. 이전 기본값은 OpenAI
  URL이 비OpenAI 클라이언트(예: Gemini)로 새어 들어가, `backend_url`을
  재정의하지 않고 프로바이더를 바꾼 Python 사용자에게 잘못된 요청 URL을
  만들었습니다. CLI 흐름에는 영향이 없습니다.
- 모든 파일 I/O가 명시적 `encoding="utf-8"`을 전달하므로, Windows 사용자가
  cp1252 기본값으로 인한 `UnicodeEncodeError`를 더 이상 겪지 않습니다.
  (#543, #550, #576)
- Docker 권한 문제 해결을 위해 캐시·로그 디렉터리가 `~/.tradingagents/`로
  이동했습니다. (#519)
- `SignalProcessor`가 포트폴리오 매니저의 렌더링된 마크다운에서 결정론적
  휴리스틱으로 투자의견을 읽습니다 — 추가 LLM 호출이 없습니다.
- OpenAI 구조화 출력 호출이 기본적으로 `method="function_calling"`을
  사용합니다. langchain-openai의 Responses API 파싱 경로가 내보내는 시끄러운
  `PydanticSerializationUnexpectedValue` 경고를 피하기 위함이며, 결과 타입은
  동일하고 경고만 사라집니다.

### 수정 (Fixed)

- 빈 메모리가 더 이상 에이전트 프롬프트에서 지어낸 과거 교훈을 유발하지
  않습니다. 메모리 로그 재설계로 포트폴리오 매니저만, 그것도 항목이 존재할
  때만 메모리를 참조하므로 구조적으로 불가능해졌습니다. (#572)
- 도구 호출 로깅이 마지막 메시지만이 아니라 모든 청크 메시지를 처리하고,
  메모리 점수 정규화가 빈 점수 배열을 처리합니다. (#534, #531)

### 제거 (Removed)

- `FinancialSituationMemory`(에이전트별 BM25 시스템)와 사용되지 않는
  `reflect_and_remember()` 관련 코드 — 영속 의사결정 로그로 대체되었습니다.
- `langchain-google-genai`가 API 경로를 바꿨을 때 404를 유발하던 하드코딩된
  Google 엔드포인트. (#493, #496)

### 기여자 (Contributors)

코드, 설계, 리포트로 이번 릴리스에 기여해 주신 모든 분께 감사드립니다:

- [@claytonbrown](https://github.com/claytonbrown) — 체크포인트 재개 (#594), 테스트 픽스처 (#588), 비용 추적(#582)과 구조화 검증(#583)에 대한 설계 피드백
- [@Bcardo](https://github.com/Bcardo) — 메모리 로그 재설계 (#579), 빈 메모리 환각 리포트 (#572), 인코딩 수정 제안 (#570)
- [@voidborne-d](https://github.com/voidborne-d) — 메모리 영속화 설계 (#564), 포트폴리오 매니저 상태 수정 (#503)
- [@mannubaveja007](https://github.com/mannubaveja007) — 구조화 출력 기능 요청 (#434)
- [@kelder66](https://github.com/kelder66) — RAM 전용 메모리 이슈 (#563)
- [@Gujiassh](https://github.com/Gujiassh) — 도구 호출 로깅 수정 (#534), 테스트 스텁 PR (#533)
- [@iuyup](https://github.com/iuyup) — 메모리 점수 정규화 수정 (#531)
- [@kaihg](https://github.com/kaihg) — Google base_url 수정 (#496)
- [@32ryh98yfe](https://github.com/32ryh98yfe) — Gemini 404 리포트 (#493)
- [@uppb](https://github.com/uppb) — OpenRouter 동적 모델 선택 (#482)
- [@guoz14](https://github.com/guoz14) — OpenRouter 모델 제한 리포트 (#337)
- [@samchenku](https://github.com/samchenku) — 지표 이름 정규화 (#490)
- [@JasonOA888](https://github.com/JasonOA888) — y_finance pandas 임포트 수정 (#488)
- [@tiffanychum](https://github.com/tiffanychum) — 사용되지 않는 임포트 정리 (#499)
- [@zaizou](https://github.com/zaizou) — Docker 권한 이슈 (#519)
- [@Stosman123](https://github.com/Stosman123), [@mauropuga](https://github.com/mauropuga), [@hotwind2015](https://github.com/hotwind2015) — Windows 인코딩 버그 리포트 (#543, #550, #576)
- [@nnishad](https://github.com/nnishad), [@atharvajoshi01](https://github.com/atharvajoshi01) — 인코딩 수정 제안 (#568, #549)

## [0.2.3] — 2026-03-29

### 추가 (Added)

- **분석가 보고서와 최종 의사결정의 다국어 출력** — CLI 선택기가 함께
  제공됩니다. 추론 품질을 위해 에이전트 간 내부 토론은 영어로 유지됩니다. (#472)
- **GPT-5.4 계열 모델**을 기본 카탈로그에 추가 — deep/quick 모델 분리 적용.
- **통합 모델 카탈로그** — CLI 옵션과 프로바이더 검증의 단일 기준 소스(single
  source of truth)가 됩니다.

### 변경 (Changed)

- `base_url`이 Google과 Anthropic 클라이언트에도 전달되어 회사 프록시가 모든
  프로바이더에서 일관되게 동작합니다. (#427)
- Google의 `api_key` 파라미터를 통일된 `api_key` 형태로 표준화했습니다.

### 수정 (Fixed)

- `curr_date`가 조회된 기간의 중간에 있을 때 백테스팅 페처(fetcher)가 미래
  참조 데이터를 더 이상 누출하지 않습니다. (#475)
- LLM이 잘못된 지표 이름을 생성해도 실행이 크래시되지 않고 도구 경계에서
  잡아냅니다. (#429)
- yfinance 뉴스 페처가 가격 페처와 동일한 지수 백오프(exponential backoff)
  재시도를 따릅니다. (#445)

### 기여자 (Contributors)

- [@ahmedk20](https://github.com/ahmedk20) — 다국어 출력 (#472)
- [@CadeYu](https://github.com/CadeYu) — 모델 카탈로그 타입 정리 (#464)
- [@javierdejesusda](https://github.com/javierdejesusda) — Google API 키 파라미터 통일 (#453)
- [@voidborne-d](https://github.com/voidborne-d) — yfinance 뉴스 재시도 (#445)
- [@kostakost2](https://github.com/kostakost2) — 미래 참조 편향(look-ahead bias) 리포트 (#475)
- [@lu-zhengda](https://github.com/lu-zhengda) — 프록시/base_url 지원 요청 (#427)
- [@VamsiKrishna2021](https://github.com/VamsiKrishna2021) — 잘못된 지표 크래시 리포트 (#429)

## [0.2.2] — 2026-03-22

### 추가 (Added)

- **5단계 투자의견 체계** (Buy / Overweight / Hold / Underweight / Sell)를
  포트폴리오 매니저에 도입했습니다.
- Claude 모델을 위한 **Anthropic effort 수준** 지원.
- 네이티브 OpenAI 모델을 위한 **OpenAI Responses API** 경로.

### 변경 (Changed)

- CLI 화면에 표시되는 역할 설명과 일치하도록 `risk_manager`를
  `portfolio_manager`로 이름을 변경했습니다.
- 거래소가 표기된 티커(예: `7203.T`, `BRK.B`)가 모든 에이전트 프롬프트와 도구
  호출에서 보존됩니다.
- 크로스 플랫폼 일관성을 위해 프로세스 수준 UTF-8 기본값을 시도했습니다
  (참고: 이 접근은 실제로 효과가 없었으며, v0.2.4에서 호출마다 명시적
  `encoding="utf-8"` 인자를 넘기는 방식으로 대체되었습니다).

### 수정 (Fixed)

- yfinance 요청 제한(rate-limit) 오류가 지수 백오프로 재시도됩니다. (#426)
- 커스텀 인증서 번들이 필요한 환경을 위해 HTTP 클라이언트 SSL 커스터마이징을
  지원합니다. (#379)
- 보고서 섹션 쓰기가 문자열 리스트 형태의 내용을 안전하게 처리합니다.

### 기여자 (Contributors)

- [@CadeYu](https://github.com/CadeYu) — 거래소 표기 티커 보존 (#413)
- [@yang1002378395-cmyk](https://github.com/yang1002378395-cmyk) — HTTP 클라이언트 SSL 커스터마이징 (#379)

## [0.2.1] — 2026-03-15

### 보안 (Security)

- `langchain-core` 취약점(LangGrinch)을 패치했습니다. (#335)
- CVE-2026-22218의 영향을 받는 `chainlit` 의존성을 제거했습니다.

### 추가 (Added)

- `pyproject.toml` 빌드 시스템 구성 — 프로젝트가 이제 최신 패키징 도구로
  설치됩니다.

### 제거 (Removed)

- `setup.py` — 의존성이 `pyproject.toml`로 통합되었습니다.

### 수정 (Fixed)

- 리스크 매니저가 올바른 펀더멘털 보고서 소스를 읽습니다. (#341)
- 모든 `open()` 호출이 명시적 UTF-8 인코딩을 받습니다(1차 적용).
- `get_indicators` 도구가 LLM이 쉼표로 구분해 보낸 지표 이름을 처리합니다. (#368)
- `Propagation`이 모든 토론 상태 필드를 초기화하므로 리스크 토론자가 누락된
  키를 만나지 않습니다.
- 주가 데이터 파싱이 손상된 CSV와 NaN 값을 허용합니다.
- 조건부 토론 로직이 설정된 라운드 수를 존중합니다. (#361)

### 기여자 (Contributors)

- [@RinZ27](https://github.com/RinZ27) — `langchain-core` 보안 패치 (#335)
- [@Ljx-007](https://github.com/Ljx-007) — 리스크 매니저 펀더멘털 보고서 수정 (#341)
- [@makk9](https://github.com/makk9) — 토론 라운드 설정 이슈 (#361)

## [0.2.0] — 2026-02-04

최초 공개 버전 이후 가장 큰 릴리스입니다. 프레임워크가 단일 프로바이더에서
멀티 프로바이더 아키텍처로 전환되었고, 실사용 수준의 기능들이 여럿
추가되었습니다.

### 추가 (Added)

- **멀티 프로바이더 LLM 지원** (OpenAI, Google, Anthropic, xAI, OpenRouter,
  Ollama) — 팩토리 패턴 기반이며, 프로바이더별 thinking 설정을 지원합니다.
- **Alpha Vantage** 통합 — 설정 가능한 기본 데이터 프로바이더로, 커뮤니티
  안정성을 위한 폴백으로 yfinance를 사용합니다.
- **CLI 하단 통계** — LangChain 콜백을 통한 LLM 호출, 도구 호출, 토큰
  사용량의 실시간 추적.
- **분석 후 보고서 저장** — 실행 완료 시 섹션별 마크다운 파일(분석가 보고서,
  토론 기록, 최종 의사결정)을 저장합니다.
- **공지 패널** — CLI 시작 화면에 표시할 업데이트를
  `api.tauric.ai/v1/announcements`에서 가져옵니다.
- **도구 폴백** — 단일 벤더 장애가 파이프라인을 멈추지 않도록 합니다.

### 변경 (Changed)

- Risky / Safe 리스크 토론자의 이름을 화면에 표시되는 에이전트 라벨과
  일치하도록 **Aggressive / Conservative(공격적 / 보수적)**로 변경했습니다.
- 커뮤니티 배포 환경 전반의 안정성과 쿼터(사용량 한도) 균형을 위해 기본
  데이터 벤더를 교체했습니다.
- Ollama와 OpenRouter 모델 목록을 갱신하고 기본 엔드포인트를 명확히 했습니다.

### 수정 (Fixed)

- 실시간 화면에서 분석가 상태 추적과 메시지 중복 제거를 수정했습니다.
- 에이전트 루프에 무한 루프 방지 장치를 추가하고, 회고와 로깅을 강화했습니다.
- 여러 데이터 벤더 구현 버그와 도구 시그니처 불일치를 수정했습니다.

### 기여자 (Contributors)

외부 기여가 본격적으로 반영된 첫 릴리스이며, 2025년 말의 여러 커뮤니티 PR도
여기에 포함되었습니다.

- [@luohy15](https://github.com/luohy15) — Alpha Vantage 데이터 벤더 통합 (#235)
- [@EdwardoSunny](https://github.com/EdwardoSunny) — yfinance 조회 최적화 (#245)
- [@Mirza-Samad-Ahmed-Baig](https://github.com/Mirza-Samad-Ahmed-Baig) — 무한 루프 방지, 회고 및 로깅 수정 (#89)
- [@ZeroAct](https://github.com/ZeroAct) — 결과 저장 경로 지원 (#29)
- [@Zhongyi-Lu](https://github.com/Zhongyi-Lu) — `.env` gitignore (#49)
- [@csoboy](https://github.com/csoboy) — 로컬 Ollama 설정 (#53)
- [@chauhang](https://github.com/chauhang) — 최초 Docker 지원 시도 (#47, 이후 되돌려짐; 병합된 Docker 지원은 v0.2.4에 포함)

## [0.1.1] — 2025-06-07

### 제거 (Removed)

- v0.1.0에 함께 포함되어 있던 정적 사이트 자산 — 공개 사이트는 이제 별도로
  운영됩니다.

## [0.1.0] — 2025-06-05

### 추가 (Added)

- TradingAgents 멀티 에이전트 트레이딩 프레임워크의 **최초 공개 릴리스**:
  마켓 / 감성 / 뉴스 / 펀더멘털 분석가, 강세·약세 리서처, 트레이더,
  공격적·보수적·중립적 리스크 토론자, 포트폴리오 매니저. LangGraph
  오케스트레이션, yfinance 데이터, 에이전트별 BM25 메모리(키워드 기반 검색
  메모리), 단일 프로바이더 OpenAI 통합, 대화형 CLI.

[0.2.4]: https://github.com/TauricResearch/TradingAgents/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/TauricResearch/TradingAgents/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/TauricResearch/TradingAgents/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/TauricResearch/TradingAgents/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/TauricResearch/TradingAgents/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/TauricResearch/TradingAgents/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/TauricResearch/TradingAgents/releases/tag/v0.1.0
