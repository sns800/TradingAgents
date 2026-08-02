# TradingAgents 웹 UI — AWS 서비스 및 비용

> 이 문서는 웹 UI(https://stock.happymstn.com)를 **운영·실행하는 데 쓰이는 AWS
> 서비스 목록과 월 예상 비용**을 정리합니다. 구조·사용법은 [아키텍처.md](아키텍처.md),
> 변경 이력은 [변경이력.md](변경이력.md) 참고.

## 비용 추적 (태그)

이 시스템의 모든 리소스에는 **`Project=stock`** 태그가 붙어 있습니다
(CloudFormation 스택 레벨 태그로 자동 전파 + 스택 밖 ACM 인증서 개별 태깅).
Cost Explorer에서 이 태그로 필터링하면 프로젝트 전체 비용을 한눈에 볼 수 있습니다.

```bash
# 배포 시 태그는 deploy.sh가 자동 부여 (--tags Project=stock Application=tradingagents-webui)
# 태그 확인 예:
aws lambda list-tags --region ap-northeast-2 \
  --resource arn:aws:lambda:ap-northeast-2:<ACCOUNT>:function:tradingagents-webui-api
```

> ⚠️ **최초 1회 필요**: 사용자 정의 태그는 Billing 콘솔 → **Cost allocation tags**에서
> `Project`를 **활성화(Activate)** 해야 청구서·Cost Explorer에 집계됩니다. 태그가 청구
> 시스템에 나타나기까지 최대 24시간 걸리므로, 태그 부여 다음 날 활성화하세요.
> (CLI: `aws ce update-cost-allocation-tags-status --cost-allocation-tags-status TagKey=Project,Status=Active`)

> ℹ️ **Bedrock은 태그로 잡히지 않습니다** — LLM 호출(InvokeModel)은 us-east-1의
> *사용량*이라 리소스 태그가 없습니다. Cost Explorer에서 **서비스=Bedrock, 리전=us-east-1**
> 로 필터링해 추적하세요 (계정에서 Bedrock을 이 프로젝트만 쓴다면 그 값이 곧 이 비용).

---

## AWS 서비스 목록

### 서울 리전 (ap-northeast-2) — 시스템 운영

| 서비스 | 리소스 | 역할 |
|---|---|---|
| **CloudFront** | 배포판 1개 (+OAC 2, CF 함수) | 유일한 진입점, 정적 사이트·API 라우팅 |
| **S3** | 사이트 버킷 + 데이터 버킷 | SPA 정적 파일 / 분석 보고서·종목 카탈로그·빌드 소스 |
| **Lambda** | 함수 1개 (+Function URL) | REST API (실행 생성·조회, 카탈로그, 티커 검증) |
| **DynamoDB** | 테이블 1개 (온디맨드) | 분석 실행 상태·메타데이터 |
| **ECS Fargate** | 클러스터 1 + 태스크 정의 1 (1 vCPU / 4 GB) | 분석 워커(실행당 1태스크) + 일일 카탈로그 배치 |
| **ECR** | 리포지토리 1개 (이미지 ~181 MB) | 워커 컨테이너 이미지 |
| **EventBridge** | 규칙 1개 | 평일 22:00 KST 카탈로그 배치 트리거 |
| **CodeBuild** | 프로젝트 1개 | 워커 이미지 빌드 (로컬 Docker 대체) |
| **CloudWatch Logs** | 로그 그룹 (30일 보존) | 워커·Lambda 로그 |
| **IAM** | 역할 5개 | 최소 권한 실행 역할 (과금 없음) |

### us-east-1 — LLM 및 인증서

| 서비스 | 역할 | 비고 |
|---|---|---|
| **Bedrock** | Claude Sonnet 4.5(심층)·Haiku 4.5(빠름) 호출 | **변동비의 대부분** |
| **ACM** | CloudFront용 TLS 인증서 (stock.happymstn.com) | 무료 |

### 공유 리소스 (이 프로젝트 전용 아님 — 비용 대부분 기존 앱과 분담)

| 서비스 | 비고 |
|---|---|
| **Route53** | happymstn.com 호스팅 존 — 가계부·rtms와 공유 (존 $0.50/월은 기존 비용) |
| **Cognito** | household-ledger-users 풀 공유 — 무료 티어(월 50k MAU) 내 |

---

## 월 예상 비용

> 서울 리전 기준 대략치이며(요금은 2026-08 기준), **실제 비용은 분석 실행 횟수와
> 깊이에 좌우**됩니다. 상시 고정비는 1달러 미만이고, 비용의 대부분은 분석마다
> 발생하는 **Bedrock 토큰 요금**입니다.

### 고정비 (분석을 한 건도 안 돌려도 나가는 비용)

| 항목 | 월 비용 | 근거 |
|---|---|---|
| ECR 이미지 저장 | ~$0.02 | 181 MB × $0.10/GB |
| S3 저장 | ~$0.01 미만 | 사이트+데이터 ~8 MB |
| 일일 카탈로그 배치 (Fargate) | ~$0.20 | 평일 22회 × ~8분 × (1vCPU+4GB) |
| CloudFront / Lambda / DynamoDB / CloudWatch | ~$0 | 가족 사용량은 대부분 프리티어 내 |
| **고정비 합계** | **약 $0.5 미만/월** | (공유 Route53 $0.50 별도) |

### 변동비 (분석 1건당)

| 항목 | 얕게(depth 1) | 깊게(depth 5) |
|---|---|---|
| Fargate 컴퓨팅 | ~$0.01 (약 10분) | ~$0.05 (약 45분) |
| **Bedrock 토큰** | **~$0.5 – 1.5** | **~$2 – 4** |
| **분석 1건 합계** | **약 $0.5 – 1.5** | **약 $2 – 4** |

Bedrock 비용이 지배적입니다 — 파이프라인이 분석가 4종·토론·트레이더·리스크
토론·최종 결정까지 여러 LLM 호출을 하고, 토론 라운드(depth)가 늘수록 컨텍스트
재주입으로 토큰이 배가됩니다. 위 값은 추정 범위이며, 정확한 값은 배포 후
Cost Explorer의 Bedrock(us-east-1) 실측으로 보정하세요.

### 사용량 시나리오별 월 총액 (예시)

| 사용 패턴 | 월 예상 총액 |
|---|---|
| 유휴 (분석 0건) | **~$0.5** |
| 가벼운 사용 (얕게 30건/월) | **~$20 – 45** |
| 중간 사용 (깊게 30건/월) | **~$60 – 120** |

> 💡 **비용 상한 장치**: 동시 실행은 `MaxActiveRuns`(기본 10) 파라미터로 제한됩니다.
> 무인 상태에서 비용이 폭주하지 않도록, 낮은 값으로 배포하거나
> `MAX_ACTIVE_RUNS=3 webui/infra/deploy.sh --skip-image`로 조정하세요.
> 서버리스 구성이라 **안 쓰면 거의 0원**입니다.

---

## 배포·운영

전체 배포(인프라+이미지+API+프론트)와 운영 명령은 [아키텍처.md의 "배포/운영"](아키텍처.md) 절 참고.
요약: `webui/infra/deploy.sh` (프론트/API만 갱신 시 `--skip-image`).
