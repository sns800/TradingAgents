# 1단계(builder): 가상환경을 만들고 패키지를 설치합니다.
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
COPY . .
RUN pip install --no-cache-dir .

# 2단계(runtime): 빌드 결과물만 복사한 가벼운 실행 이미지를 만듭니다.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# 보안을 위해 root가 아닌 전용 사용자(appuser)로 실행하고,
# 결과물이 저장되는 ~/.tradingagents 디렉터리를 미리 만들어 둡니다.
RUN useradd --create-home appuser \
 && install -d -m 0755 -o appuser -g appuser /home/appuser/.tradingagents
USER appuser
WORKDIR /home/appuser/app

COPY --from=builder --chown=appuser:appuser /build .

# 컨테이너 시작 시 tradingagents CLI를 실행합니다.
ENTRYPOINT ["tradingagents"]
