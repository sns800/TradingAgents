# 웹 UI 분석 워커 이미지 (빌드 컨텍스트: 저장소 루트)
# Lambda API가 ECS Fargate RunTask로 실행하며, 태스크당 분석 1건을 처리하고 종료합니다.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY . .

# bedrock 옵션: langchain-aws + boto3 (Bedrock LLM 호출용)
RUN pip install --no-cache-dir ".[bedrock]" boto3

RUN useradd --create-home appuser \
 && install -d -m 0755 -o appuser -g appuser /home/appuser/.tradingagents
USER appuser

CMD ["python", "webui/worker/worker.py"]
