# ============================================================
# [모듈 개요] 웹 UI용 분석 워커 (ECS Fargate 태스크 진입점)
#
# Lambda API가 ECS RunTask로 이 컨테이너를 띄우면, DynamoDB에서 실행
# 파라미터(종목/날짜/깊이)를 읽어 TradingAgents 그래프를 실행하고,
# 보고서를 S3에 업로드한 뒤 상태를 DynamoDB에 기록합니다.
#
# 리전 구성:
#  - Bedrock LLM 호출: us-east-1 (컨테이너의 AWS_REGION/AWS_DEFAULT_REGION)
#  - DynamoDB/S3 등 시스템 리소스: 서울(ap-northeast-2), HOME_REGION 환경변수로
#    명시해 boto3 클라이언트에 직접 넘긴다 (Bedrock용 리전 env와 분리).
# ============================================================
import contextlib
import os
import sys
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path

import boto3

RUN_ID = os.environ["RUN_ID"]
TABLE_NAME = os.environ["TABLE_NAME"]
DATA_BUCKET = os.environ["DATA_BUCKET"]
HOME_REGION = os.environ.get("HOME_REGION", "ap-northeast-2")

DEEP_MODEL = os.environ.get(
    "BEDROCK_DEEP_MODEL", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
)
QUICK_MODEL = os.environ.get(
    "BEDROCK_QUICK_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0"
)

dynamodb = boto3.resource("dynamodb", region_name=HOME_REGION)
table = dynamodb.Table(TABLE_NAME)
s3 = boto3.client("s3", region_name=HOME_REGION)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def update_run(**fields):
    """실행 레코드의 필드들을 갱신한다 (updated_at은 항상 함께 갱신)."""
    fields["updated_at"] = now_iso()
    expr = ", ".join(f"#k{i} = :v{i}" for i in range(len(fields)))
    names = {f"#k{i}": k for i, k in enumerate(fields)}
    values = {f":v{i}": v for i, v in enumerate(fields.values())}
    table.update_item(
        Key={"run_id": RUN_ID},
        UpdateExpression=f"SET {expr}",
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def heartbeat_loop(stop_event: threading.Event):
    """실행 중임을 알리는 하트비트. UI가 '아직 살아있음'을 알 수 있게 한다."""
    while not stop_event.wait(30):
        # 하트비트 실패는 치명적이지 않으므로 조용히 넘어간다
        with contextlib.suppress(Exception):
            update_run(status="running")


def upload_reports(report_dir: Path) -> list[str]:
    """보고서 디렉터리 전체를 S3의 runs/{run_id}/reports/ 아래로 업로드한다."""
    uploaded = []
    for path in sorted(report_dir.rglob("*.md")):
        rel = path.relative_to(report_dir).as_posix()
        key = f"runs/{RUN_ID}/reports/{rel}"
        s3.upload_file(
            str(path), DATA_BUCKET, key,
            ExtraArgs={"ContentType": "text/markdown; charset=utf-8"},
        )
        uploaded.append(rel)
    return uploaded


def main() -> int:
    item = table.get_item(Key={"run_id": RUN_ID}).get("Item")
    if not item:
        print(f"run {RUN_ID} not found in {TABLE_NAME}", file=sys.stderr)
        return 1

    ticker = item["ticker"]
    analysis_date = item["analysis_date"]
    depth = int(item.get("depth", 1))

    update_run(status="running", started_at=now_iso())
    stop = threading.Event()
    threading.Thread(target=heartbeat_loop, args=(stop,), daemon=True).start()

    try:
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        config = DEFAULT_CONFIG.copy()
        config["llm_provider"] = "bedrock"
        config["deep_think_llm"] = DEEP_MODEL
        config["quick_think_llm"] = QUICK_MODEL
        config["backend_url"] = None
        config["max_debate_rounds"] = depth
        config["max_risk_discuss_rounds"] = depth
        # output_language는 이 포크의 기본값(Korean)을 그대로 사용

        # CLI와 동일하게 티커에서 자산 유형(주식/암호화폐)을 자동 감지
        from cli.utils import detect_asset_type, normalize_ticker_symbol
        canonical = normalize_ticker_symbol(ticker)
        asset_type = detect_asset_type(canonical).value

        graph = TradingAgentsGraph(debug=False, config=config)
        final_state, decision = graph.propagate(
            canonical, analysis_date, asset_type=asset_type
        )

        report_dir = Path("/tmp/reports")
        graph.save_reports(final_state, canonical, save_path=report_dir)
        reports = upload_reports(report_dir)

        update_run(
            status="completed",
            decision=str(decision) if decision is not None else None,
            reports=reports,
            finished_at=now_iso(),
        )
        print(f"run {RUN_ID} completed: {decision}")
        return 0
    except Exception as e:
        traceback.print_exc()
        update_run(
            status="failed",
            error=f"{type(e).__name__}: {e}"[:1000],
            finished_at=now_iso(),
        )
        return 1
    finally:
        stop.set()


if __name__ == "__main__":
    sys.exit(main())
