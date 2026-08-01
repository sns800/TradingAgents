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
#  3. Lambda API 코드 배포
#  4. 프론트엔드 S3 동기화 + CloudFront 무효화
# ============================================================
set -euo pipefail

REGION="ap-northeast-2"
STACK="tradingagents-webui"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
INFRA="$ROOT/webui/infra"
SKIP_IMAGE="${1:-}"

cd "$ROOT"

# ---------- 1. 기본 VPC/서브넷 조회 ----------
VPC_ID="$(aws ec2 describe-vpcs --region "$REGION" \
  --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)"
SUBNET_IDS="$(aws ec2 describe-subnets --region "$REGION" \
  --filters Name=default-for-az,Values=true Name=vpc-id,Values="$VPC_ID" \
  --query 'Subnets[].SubnetId' --output text | tr '\t' ',')"
echo "VPC: $VPC_ID / 서브넷: $SUBNET_IDS"

# ---------- 2. CloudFormation 스택 배포 ----------
echo "== CloudFormation 스택 배포 중 (수 분 소요) =="
aws cloudformation deploy \
  --region "$REGION" \
  --stack-name "$STACK" \
  --template-file "$INFRA/template.yaml" \
  --capabilities CAPABILITY_IAM \
  --no-fail-on-empty-changeset \
  --parameter-overrides \
    "VpcId=$VPC_ID" \
    "SubnetIds=$SUBNET_IDS" \
    "MaxActiveRuns=${MAX_ACTIVE_RUNS:-10}"

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
(cd "$ROOT/webui/backend" && zip -q "$API_ZIP" api_handler.py)
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
echo "======================================================"
