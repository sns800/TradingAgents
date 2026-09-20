# 웹 UI 워커 이미지 (빌드 컨텍스트: 저장소 루트)
# Lambda API가 ECS Fargate RunTask로 실행하며, 태스크당 분석 1건을 처리하고 종료합니다.
# 이미지는 배치와 공용입니다 - 종목 카탈로그(webui/catalog/build_catalog.py)와
# G20 매크로 수집(webui/macro/collect.py)은 command만 바꿔 같은 이미지를 띄웁니다.
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
#  - openpyxl: SZSE(선전거래소) A주 목록 xlsx 읽기 (중국 카탈로그).
# G20 매크로 수집기(webui/macro/collect.py) 추가 의존성:
#  - pyyaml: 지표 레지스트리(webui/macro/registry.yaml) 로드. langchain 의존성으로
#    따라 들어올 수 있지만, 수집기가 직접 import하므로 상위 패키지 변경에
#    끌려가지 않도록 명시적으로 고정한다.
#  - lxml: 위키피디아 여론조사 표를 pandas.read_html로 파싱 (위 카탈로그용과 공유).
#  - requests/boto3: 각 통계기관 REST API 호출과 DynamoDB/S3/Bedrock 접근 (기존 의존).
RUN pip install --no-cache-dir ".[bedrock]" boto3 "xlrd>=2.0.1" "lxml>=5.0" "openpyxl>=3.1" "pyyaml>=6"

RUN useradd --create-home appuser \
 && install -d -m 0755 -o appuser -g appuser /home/appuser/.tradingagents
USER appuser

CMD ["python", "webui/worker/worker.py"]
