# 웹 UI 분석 워커 이미지 (빌드 컨텍스트: 저장소 루트)
# Lambda API가 ECS Fargate RunTask로 실행하며, 태스크당 분석 1건을 처리하고 종료합니다.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY . .

# bedrock 옵션: langchain-aws + boto3 (Bedrock LLM 호출용)
# 카탈로그 배치(webui/catalog/build_catalog.py) 추가 의존성:
#  - xlrd: JPX 상장종목일람(레거시 .xls) 읽기. xlrd 2.x는 .xlsx 지원만 제거했고
#    .xls는 계속 지원한다 (2.0.2 + pandas 3.x 조합으로 실물 파일 검증 완료).
#  - lxml: KRX KIND 상장법인목록(HTML 테이블) pandas.read_html 파싱 엔진.
RUN pip install --no-cache-dir ".[bedrock]" boto3 "xlrd>=2.0.1" "lxml>=5.0"

RUN useradd --create-home appuser \
 && install -d -m 0755 -o appuser -g appuser /home/appuser/.tradingagents
USER appuser

CMD ["python", "webui/worker/worker.py"]
