#!/usr/bin/env bash
# ============================================================
# TradingAgents 웹 UI 배포 스크립트
#
# 사용법: webui/infra/deploy.sh [--skip-image]
#   --skip-image : 워커 이미지 빌드(CodeBuild) 생략 (프론트/API만 갱신할 때)
#
# 하는 일:
#  1. CloudFormation 스택 배포 (서울 리전)
#  2. 소스 zip 업로드 -> CodeBuild로 워커 이미지 빌드/푸시
#  3. Lambda API 코드 배포 (api_handler.py + macro_api.py)
#  4. 프론트엔드 S3 동기화 + CloudFront 무효화
#
# 환경변수로 조정 가능한 스택 파라미터 (없으면 기본값 유지):
#   MAX_ACTIVE_RUNS               동시 분석 한도 (기본 10)
#   FRED_API_KEY / EMBER_API_KEY  G20 매크로 수집기의 소스 키 (없으면 그 소스만 스킵)
#   MACRO_LLM_DAILY_TOKEN_BUDGET  매크로 정성 처리 1일 토큰 상한 (기본 2000000)
#   MACRO_SCHEDULE_ENABLED        매크로 일일 수집 스케줄 ENABLED|DISABLED (기본 ENABLED)
# 위 매크로 변수는 저장소 루트 .env에 적어두면 자동으로 읽습니다
# (.env 전체를 source하지 않고 필요한 4개만 골라 읽음 - 아래 0단계).
# 예: FRED_API_KEY=xxxx webui/infra/deploy.sh --skip-image
# ============================================================
set -euo pipefail

REGION="ap-northeast-2"
STACK="tradingagents-webui"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
INFRA="$ROOT/webui/infra"
SKIP_IMAGE="${1:-}"

cd "$ROOT"

# ---------- 0. 매크로 설정값 로드 (.env가 있으면 필요한 변수만) ----------
# .env에는 이 스택과 무관한 키(로컬 개발용 LLM·데이터 키 등)가 많아 전체를
# source하면 예상 못한 변수가 셸에 섞인다. 그래서 필요한 4개만 골라 읽는다.
# 이미 셸 환경에 있는 값이 우선 - 1회성 오버라이드(`FRED_API_KEY=... deploy.sh`) 가능.
# 주의: 아래 파라미터 오버라이드는 값이 없으면 빈 문자열을 넘기므로, 키를 셸에도
# .env에도 주지 않고 배포하면 스택에 저장된 기존 키가 빈 값으로 덮어써진다
# (해당 소스만 스킵되고 다른 기능은 정상). 그래서 키는 .env에 적어두는 것을 권장.
ENV_FILE="$ROOT/.env"
if [[ -f "$ENV_FILE" ]]; then
  for KEY in FRED_API_KEY EMBER_API_KEY MACRO_LLM_DAILY_TOKEN_BUDGET MACRO_SCHEDULE_ENABLED; do
    [[ -n "${!KEY:-}" ]] && continue
    LINE="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${KEY}=" "$ENV_FILE" | tail -1 || true)"
    [[ -z "$LINE" ]] && continue
    VALUE="${LINE#*=}"                          # 첫 = 뒤 전부 (값에 =가 있어도 안전)
    VALUE="${VALUE%\"}"; VALUE="${VALUE#\"}"    # 감싼 따옴표만 제거
    VALUE="${VALUE%\'}"; VALUE="${VALUE#\'}"
    export "$KEY=$VALUE"
    echo ".env에서 $KEY 읽음"
  done
fi

# ---------- 1. 기본 VPC/서브넷 조회 ----------
VPC_ID="$(aws ec2 describe-vpcs --region "$REGION" \
  --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)"
SUBNET_IDS="$(aws ec2 describe-subnets --region "$REGION" \
  --filters Name=default-for-az,Values=true Name=vpc-id,Values="$VPC_ID" \
  --query 'Subnets[].SubnetId' --output text | tr '\t' ',')"
echo "VPC: $VPC_ID / 서브넷: $SUBNET_IDS"

# ---------- 2. CloudFormation 스택 배포 ----------
echo "== CloudFormation 스택 배포 중 (수 분 소요) =="
# 스택 레벨 태그: 태그 가능한 모든 리소스에 자동 전파된다. 비용 할당 태그
# Project=stock 으로 Cost Explorer에서 이 프로젝트 비용만 필터링할 수 있다
# (최초 1회 Billing 콘솔에서 'Project'를 비용 할당 태그로 활성화해야 청구서에 반영됨).
aws cloudformation deploy \
  --region "$REGION" \
  --stack-name "$STACK" \
  --template-file "$INFRA/template.yaml" \
  --capabilities CAPABILITY_IAM \
  --no-fail-on-empty-changeset \
  --tags Project=stock Application=tradingagents-webui \
  --parameter-overrides \
    "VpcId=$VPC_ID" \
    "SubnetIds=$SUBNET_IDS" \
    "MaxActiveRuns=${MAX_ACTIVE_RUNS:-10}" \
    "FredApiKey=${FRED_API_KEY:-}" \
    "EmberApiKey=${EMBER_API_KEY:-}" \
    "MacroLlmDailyTokenBudget=${MACRO_LLM_DAILY_TOKEN_BUDGET:-2000000}" \
    "MacroScheduleEnabled=${MACRO_SCHEDULE_ENABLED:-ENABLED}"

outputs() {
  aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}
URL="$(outputs CloudFrontURL)"
DIST_ID="$(outputs DistributionId)"
SITE_BUCKET="$(outputs SiteBucketName)"
DATA_BUCKET="$(outputs DataBucketName)"
API_FN="$(outputs ApiFunctionName)"
BUILD_PROJECT="$(outputs CodeBuildProject)"

# ---------- 3. 워커 이미지 빌드 (CodeBuild) ----------
if [[ "$SKIP_IMAGE" != "--skip-image" ]]; then
  echo "== 소스 업로드 및 워커 이미지 빌드 중 (약 5-10분) =="
  TMP_DIR="$(mktemp -d)"
  TMP_ZIP="$TMP_DIR/source.zip"
  git ls-files -co --exclude-standard | zip -q "$TMP_ZIP" -@
  aws s3 cp --region "$REGION" --only-show-errors "$TMP_ZIP" "s3://$DATA_BUCKET/source/source.zip"
  rm -rf "$TMP_DIR"

  BUILD_ID="$(aws codebuild start-build --region "$REGION" \
    --project-name "$BUILD_PROJECT" --query 'build.id' --output text)"
  echo "CodeBuild 시작: $BUILD_ID"
  while true; do
    STATUS="$(aws codebuild batch-get-builds --region "$REGION" --ids "$BUILD_ID" \
      --query 'builds[0].buildStatus' --output text)"
    [[ "$STATUS" != "IN_PROGRESS" ]] && break
    sleep 20
  done
  if [[ "$STATUS" != "SUCCEEDED" ]]; then
    echo "이미지 빌드 실패: $STATUS (CodeBuild 콘솔에서 로그 확인)" >&2
    exit 1
  fi
  echo "워커 이미지 빌드 완료"
fi

# ---------- 4. Lambda API 코드 배포 ----------
echo "== Lambda API 코드 배포 중 =="
API_TMP="$(mktemp -d)"
API_ZIP="$API_TMP/api.zip"
# G20 매크로 API는 별도 모듈(macro_api.py)로 분리되어 api_handler가 /api/macro/*를
# 위임한다. 구현 전이거나 브랜치에 없으면 경고만 남기고 기존 파일만 배포해
# (매크로 라우트는 404가 되지만) 나머지 API 배포는 막히지 않게 한다.
API_FILES=(api_handler.py)
if [[ -f "$ROOT/webui/backend/macro_api.py" ]]; then
  API_FILES+=(macro_api.py)
else
  echo "  경고: webui/backend/macro_api.py 없음 - 매크로 API 제외하고 배포합니다" >&2
fi
(cd "$ROOT/webui/backend" && zip -q "$API_ZIP" "${API_FILES[@]}")
aws lambda update-function-code --region "$REGION" \
  --function-name "$API_FN" --zip-file "fileb://$API_ZIP" --query 'LastModified' --output text
rm -rf "$API_TMP"

# ---------- 5. 프론트엔드 배포 ----------
echo "== 프론트엔드 S3 동기화 및 CloudFront 무효화 =="
# no-cache: 브라우저가 매번 재검증하게 해 배포 직후 구버전이 남지 않도록 함
# (ETag 기반 304 응답이라 실제 트래픽 부담은 거의 없음)
aws s3 sync --region "$REGION" "$ROOT/webui/frontend/" "s3://$SITE_BUCKET/" --delete --cache-control "no-cache"
aws cloudfront create-invalidation --distribution-id "$DIST_ID" \
  --paths "/*" --query 'Invalidation.Id' --output text

echo ""
echo "======================================================"
echo " 배포 완료!"
echo " URL   : $URL"
echo " 로그인 : Cognito 가족 계정 (가계부와 동일한 이메일/비밀번호)"
echo " 매크로 : 매일 06:00 KST 자동 수집 · 수동 실행은 webui/macro/README.md의 run-task 명령"
echo "======================================================"
