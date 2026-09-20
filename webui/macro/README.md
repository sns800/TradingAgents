# G20 매크로 수집기 — 운영 가이드

> 20개국(G20 19개국 + 유로존) 정치·통화·경제구조·재정·시장 데이터를 매일 모아
> DynamoDB `tradingagents-webui-macro`와 S3 `macro/*`에 적재하는 배치의 운영 문서입니다.
>
> - 상세 구조·작업내역: [구조와-작업내역.md](구조와-작업내역.md)
> - 세부 구현 방안(함수·알고리즘·엣지 케이스): [구현-상세.md](구현-상세.md) · 프로젝트 이력: [../../HISTORY.md](../../HISTORY.md)
> - 키·스키마·API 형식의 **단일 기준**: [CONTRACT.md](CONTRACT.md)
> - 설계 배경·화면 설계: [../G20-매크로-대시보드-제안.md](../G20-매크로-대시보드-제안.md)
> - 인프라 구조·비용: [../아키텍처.md](../아키텍처.md) 5장
>
> ⚠️ 이 문서의 CLI 옵션은 CONTRACT 9·12장을 기준으로 먼저 작성한 것입니다.
> **구현과 다르면 `collect.py --help`가 우선**입니다(수집기 구현 중).

---

## 1. 구조

```
webui/macro/
├── collect.py        진입점. 소스별 실패 격리 → 파생 계산 → LLM → LATEST/SNAPSHOT 재생성 → INGEST 기록
├── registry.yaml     지표 사전(국가 20개 + 지표 40여 개의 단위·빈도·집계규칙·소스 우선순위)
├── schema.py         Observation/Doc/Country/Indicator dataclass (CONTRACT 3·5장과 1:1)
├── aggregate.py      M/Q/Y 집계·YoY·지수화 (순수 함수)
├── store.py          DynamoDB/S3 쓰기 (batch upsert, 개정 이력, float→Decimal 변환)
├── sources/          정량 소스: bis, oecd, worldbank, imf, fred, ember, owid, wits, yahoo_fx, companies
└── llm/              정성 소스: cb_statements(중앙은행 성명), polls_wiki(여론조사 표), energy_policy, weekly_brief
```

- 실행 순서(고정): **정량 소스 → 파생(yoy·index·fuel_dep) → LLM 소스 → poll_of_polls 계산 →
  LATEST/SNAPSHOT 재생성 → INGEST 기록**. 앞 단계가 실패해도 뒤 단계는 있는 데이터로 진행합니다.
- 소스 하나가 죽어도 나머지는 계속합니다(카탈로그 배치와 동일한 실패 격리).
  실패는 `ctx.errors`에 모아 `INGEST#<source>` 항목에 남습니다.
- EventBridge `tradingagents-webui-macro-daily`가 **매일 06:00 KST(21:00 UTC 전날)** 1회만 기동하고,
  소스별 실제 갱신 주기(일·주·월·분기)는 collect.py가 `INGEST#<source>`의 `next_due`로 판단합니다.

| 소스 | 케이던스 | 소스 | 케이던스 |
|---|---|---|---|
| yahoo_fx(환율·DXY) | 매일 | oecd(CPI·PPI), imf(M2), fred(미국 폴백) | 주 1회 |
| bis(정책금리·환율·집값) | 매일(변경 감지) | ember, owid, worldbank, wits, companies | 월~분기 1회 |
| cb_statements(LLM) | 매일 확인, 신규 문서만 LLM | polls_wiki·energy_policy·weekly_brief(LLM) | 주 1회 |

---

## 2. 실행 방법

### 로컬 dry-run (AWS에 쓰지 않음)

`--dry-run`이면 DynamoDB 쓰기를 건너뛰고 원본은 `./.macro_raw/`에 떨어집니다. 소스 하나를
새로 붙였을 때 파싱이 맞는지 확인하는 용도입니다.

```bash
.venv/bin/python webui/macro/collect.py --dry-run --sources bis,worldbank --countries KR,US
```

| 옵션 | 뜻 |
|---|---|
| `--dry-run` | 읽기만. DynamoDB 미기록, 원본은 로컬 `./.macro_raw/` |
| `--sources a,b` | 지정 소스만 실행 (기본: 케이던스가 도래한 전체) |
| `--countries KR,US` | 지정 국가만 (기본: 20개 전부) |
| `--force` | `next_due`를 무시하고 강제 수집 (수동 실행·백필) |
| `--since YYYY-MM-DD` | 증분 시작일 지정 (과거 재수집) |
| `--no-llm` | LLM 단계 생략(원문 수집·정량만). 비용 0 |

### 운영 수동 실행 (Fargate run-task)

이미지·태스크 정의는 분석 워커와 **공용**이고 `command`만 바꿔 띄웁니다. 서브넷·보안그룹은
배포된 Lambda 환경변수에서 그대로 읽어 스케줄 규칙과 동일한 네트워크 구성을 씁니다.

```bash
REGION=ap-northeast-2
read -r SUBNETS SG <<<"$(aws lambda get-function-configuration --region $REGION \
  --function-name tradingagents-webui-api \
  --query 'Environment.Variables.[SUBNET_IDS,SECURITY_GROUP]' --output text)"

aws ecs run-task --region $REGION \
  --cluster tradingagents-webui \
  --task-definition tradingagents-webui-worker \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=ENABLED}" \
  --overrides '{"containerOverrides":[{"name":"worker","command":["python","webui/macro/collect.py","--force","--sources","bis"]}]}' \
  --query 'tasks[0].taskArn' --output text
```

- 전체 소스를 강제 수집하려면 `"--sources","bis"` 부분을 지우고 `["python","webui/macro/collect.py","--force"]`만 남깁니다.
- 웹 UI 관리자 탭의 **수동 재수집** 버튼(`POST /api/admin/macro/refresh {sources:[...]}`)이 위와 같은 RunTask를 대신 실행합니다.
- 진행 상황: `aws logs tail /ecs/tradingagents-webui-worker --region ap-northeast-2 --follow`

### 스케줄 끄기/켜기

수집기 개발 중이거나 비용을 멈추려면 스택 파라미터로 전환합니다(코드 변경 없음).

```bash
MACRO_SCHEDULE_ENABLED=DISABLED webui/infra/deploy.sh --skip-image
```

---

## 3. API 키 설정

키가 필요한 소스는 **키가 없으면 그 소스만 건너뛰고 경고 한 줄**을 남깁니다(예외로 죽지 않음).
BIS·OECD·World Bank·IMF·OWID·WITS는 키가 필요 없어, 키 없이도 대시보드 대부분이 채워집니다.

| 변수 | 소스 | 없을 때 |
|---|---|---|
| `FRED_API_KEY` | fred (미국 지표 폴백) | 미국 값은 BIS·OECD 경로로만 채움 |
| `EMBER_API_KEY` | ember (전력 구성 `elec_mix`) | 전력 구성 카드 비어 있음(OWID로 일부 대체) |

배포 시 env로 주입하면 CloudFormation **NoEcho 파라미터**로 들어가 워커 태스크 환경변수에만 전달됩니다.

> ⚠️ **NoEcho는 CloudFormation 콘솔·`describe-stacks`에서만 값을 가립니다.** 키는 결국 ECS 태스크 정의의
> 컨테이너 환경변수(`Environment`)로 저장되므로 `ecs:DescribeTaskDefinition` 권한이 있으면 콘솔·CLI에서
> **평문으로 보입니다**(`aws ecs describe-task-definition --task-definition tradingagents-webui-worker
> --query 'taskDefinition.containerDefinitions[0].environment'`). 워커 로그에는 키를 찍지 않지만,
> 태스크 정의를 읽을 수 있는 IAM 주체는 곧 키를 읽을 수 있는 주체라고 보고 권한을 관리하세요.
>
> **후속 항목(미구현):** SSM Parameter Store `SecureString`(또는 Secrets Manager)에 키를 두고 태스크 정의의
> `Secrets:`(`ValueFrom: <파라미터 ARN>`)로 주입하면 태스크 정의에는 ARN만 남고 실행 시점에만 복호화됩니다.
> 이 경우 워커 실행 역할에 `ssm:GetParameters`·`kms:Decrypt`가 추가로 필요합니다.

```bash
# 저장소 루트 .env에 적어두면 deploy.sh가 필요한 변수만 골라 읽습니다
#   FRED_API_KEY=xxxxxxxx
#   EMBER_API_KEY=yyyyyyyy
webui/infra/deploy.sh --skip-image

# 또는 1회성 주입
FRED_API_KEY=xxxx EMBER_API_KEY=yyyy webui/infra/deploy.sh --skip-image
```

> ⚠️ 배포 스크립트는 키가 비어 있으면 **빈 문자열을 스택 파라미터로 넘깁니다**.
> 즉 키를 `.env`나 셸에 주지 않고 `deploy.sh`를 다시 돌리면 이전에 넣은 키가 지워지고
> 그 소스만 조용히 스킵됩니다. 키는 **저장소 루트 `.env`에 적어두세요**(`.gitignore` 대상).

> 키를 바꿨는데 반영이 안 되면 태스크 정의가 갱신됐는지 확인하세요:
> `aws ecs describe-task-definition --task-definition tradingagents-webui-worker --region ap-northeast-2 --query 'taskDefinition.revision'`

---

## 4. LLM 예산

정성 처리(중앙은행 성명 기조 판정·여론조사 표 추출·에너지 정책 요약·주간 브리프)는
Bedrock **Haiku 4.5**(`MACRO_LLM_MODEL`, us-east-1)를 씁니다.

- 하루 상한은 `MACRO_LLM_DAILY_TOKEN_BUDGET`(기본 200만 토큰). 사용량은
  `CONFIG / MACRO` 항목의 `llm_tokens_used_today`·`llm_tokens_date`로 누적되고,
  상한을 넘으면 그날의 남은 LLM 단계를 건너뜁니다(정량 수집은 계속).
- **변경 감지**: 원문 해시가 이전 회차와 같으면 LLM을 호출하지 않습니다. 실제 비용은 월 $3~6 수준.
- 예산 조정: `MACRO_LLM_DAILY_TOKEN_BUDGET=500000 webui/infra/deploy.sh --skip-image`
- 현재 사용량 확인:

```bash
aws dynamodb get-item --region ap-northeast-2 \
  --table-name tradingagents-webui-macro \
  --key '{"pk":{"S":"CONFIG"},"sk":{"S":"MACRO"}}' --output json
```

---

## 5. 검토 큐 (AI 판정 문서)

AI가 만든 문서(`ai_generated: true`)는 **검토 전에도 화면에 노출**되며 `AI 판정` 배지와
`review_status`(`pending`/`approved`/`rejected`) 배지가 함께 붙습니다. 관리자가 원문 인용을 보고 승인·반려합니다.

```bash
TOKEN=$(aws cognito-idp initiate-auth --region ap-northeast-2 \
  --auth-flow USER_PASSWORD_AUTH --client-id 11ikj0bqsifr90tf4b37h4t14p \
  --auth-parameters USERNAME=<이메일>,PASSWORD=<비밀번호> \
  --query 'AuthenticationResult.AccessToken' --output text)

# 1) 검토 대상 목록 (문서 타입·국가별 최신순)
curl -s -H "x-access-token: $TOKEN" \
  "https://stock.happymstn.com/api/macro/docs?type=cb_stance&iso=KR&limit=12"

# 2) 승인/반려 (POST는 본문 SHA-256 해시 헤더 필수 — OAC 요구사항)
BODY='{"pk":"DOC#cb_stance#KR","sk":"2026-09-18#a1b2c3d4e5f6","action":"approve"}'
HASH=$(printf '%s' "$BODY" | shasum -a 256 | cut -d' ' -f1)
curl -s -X POST https://stock.happymstn.com/api/admin/macro/review \
  -H "x-access-token: $TOKEN" -H "content-type: application/json" \
  -H "x-amz-content-sha256: $HASH" -d "$BODY"
```

`action`은 `approve` | `reject`, 선택 `note`. 반려해도 문서는 남고(감사용) 화면에서 `반려` 배지가 붙습니다.

---

## 6. 장애 대응

### ① 수집 로그(INGEST) 확인 — 어떤 소스가 언제 실패했는지

```bash
aws dynamodb query --region ap-northeast-2 \
  --table-name tradingagents-webui-macro \
  --key-condition-expression "pk = :p" \
  --expression-attribute-values '{":p":{"S":"INGEST#bis"}}' \
  --no-scan-index-forward --max-items 5 \
  --query 'Items[].{run:sk.S,status:status.S,obs:n_obs.N,err:errors.L[0].S}' \
  --output table
```

`sk`는 실행 시각(ISO8601)이라 `--no-scan-index-forward`로 최신부터 나옵니다.
`sk = "LATEST"` 항목에는 마지막 결과 요약과 다음 예정 시각(`next_due`)이 있습니다.
전체 소스 현황은 화면의 관리자 탭 또는 `GET /api/macro/meta`의 `ingest` 블록에서 한눈에 볼 수 있습니다.

### ② 증상별 대처

| 증상 | 확인 | 대처 |
|---|---|---|
| 특정 국가·지표만 비어 있음 | `INGEST#<source>`의 `countries_failed` | 그 소스만 `--force`로 재실행. 소스가 코드를 바꿨으면 `macro/raw/`의 원본 응답과 파서 비교 |
| 모든 값이 어제 날짜 그대로 | 워커 로그, EventBridge 규칙 `State` | 규칙이 DISABLED가 아닌지 확인 후 수동 run-task |
| 태스크가 바로 죽음 | `aws logs tail /ecs/tradingagents-webui-worker` | 대개 의존성·권한 문제. IAM은 `MacroTable`·`macro/*`만 허용돼 있어 경로 오타면 AccessDenied |
| 값이 이상하게 튐 | 관측치 상세의 `revisions`·`vintage`·`method` | 원천 개정이면 정상(개정 이력 보존). 파싱 오류면 `macro/raw/` 원본으로 재처리 |
| LLM 문서가 안 늘어남 | `CONFIG/MACRO`의 `llm_tokens_used_today` | 예산 소진이면 상한 상향 또는 다음 날 대기. 변경 감지로 스킵된 경우는 정상 |
| 잘못 적재해서 되돌려야 함 | — | 테이블은 PITR 활성(35일). 복원은 새 테이블로 받아 비교 후 반영(운영 테이블 직접 덮어쓰기 금지) |

데이터는 스택을 지워도 남습니다(`DeletionPolicy: Retain`) — 30년치 재수집에 수 시간이 걸리기 때문입니다.

| 증상 | 확인 | 대처 |
|---|---|---|
| 수동 재수집이 `409 이미 수집이 실행 중입니다` | `CONFIG / LOCK#collect` 항목의 `holder`·`expires_at` | 정상 실행 중이면 끝날 때까지 대기(락은 종료 시 삭제, 태스크가 죽어도 3시간 후 만료). 만료 전인데 태스크가 없으면 `aws dynamodb delete-item --key '{"pk":{"S":"CONFIG"},"sk":{"S":"LOCK#collect"}}'`로 해제 |
| 스택 재생성/교체 시 `tradingagents-webui-macro already exists` | 이전 스택이 `Retain`으로 남긴 테이블(고정 이름) | 절차: ① 스택 이벤트로 충돌 확인 → ② 기존 테이블을 **지우지 말고** 스택을 롤백 → ③ `template.yaml`의 `MacroTable`을 **이름 변경 없이** 기존 리소스 임포트(`aws cloudformation create-change-set --change-set-type IMPORT` + `ResourcesToImport`)로 다시 편입 → ④ 임포트 후 일반 업데이트. 임포트가 불가하면 새 이름으로 만들고 PITR 복원/`Export`로 옮긴다 |

---

## 7. 데이터 라이선스 — 출처 표기 의무

아래 소스는 **출처 표기를 조건으로** 재이용이 허용됩니다. 화면 하단 고정 출처 표기와
관측치 상세의 기관명·시리즈 ID·원본 URL 노출이 이 의무를 충족시키는 장치이므로 **제거하지 마세요**.

| 소스 | 표기 | 조건 |
|---|---|---|
| **BIS** (정책금리·환율·주택가격) | Bank for International Settlements | 출처 표기, 비상업적 재이용 허용 |
| **OECD** (CPI·PPI) | OECD | 출처·시리즈 표기 |
| **World Bank** (GDP·GNI·재정·부가가치·수출) | World Bank Open Data | CC BY 4.0 |
| **IMF** (통화량 등) | IMF Data | 출처 표기 |
| **Ember** (전력 구성) | Ember — Electricity Data | **CC BY 4.0** (저작자 표시 필수) |
| **OWID** (에너지·전력) | Our World in Data | CC BY 4.0 |
| **WITS/UN Comtrade** (수출 품목) | World Bank WITS | 출처 표기 |
| **위키피디아** (여론조사 표) | Wikipedia 해당 문서 링크 | CC BY-SA — 원문 링크 필수 |
| **각국 중앙은행** (성명 원문) | 기관명 + 원문 링크 | 인용 범위 준수(요약 + 짧은 인용) |
| **Yahoo Finance** (환율·시총) | 참고용 표기 | 비공식 소스 — **"참고용"** 문구 유지 |

AI가 생성한 요약·판정은 원문이 아니므로 `ai_generated` 배지, 신뢰도, 인용(`quotes`),
원문 링크를 항상 함께 노출합니다(CONTRACT 5장).
