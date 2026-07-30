# =============================================================================
# [모듈 개요 - 초보자용]
# tradingagents 패키지의 최상위 초기화 파일입니다.
# 이 패키지는 여러 LLM 에이전트가 협업해 주식/자산 매매를 결정하는 프레임워크로,
# 전체 파이프라인은 「분석가(Analyst) → 리서처 토론(Bull/Bear) → 트레이더(Trader)
# → 리스크 토론(Risk Debate) → 포트폴리오 매니저(Portfolio Manager)」 순서로 흐릅니다.
# 이 파일 자체는 에이전트가 아니라, 패키지를 import하는 순간 실행되는 준비 작업
# (.env 환경 변수 로드, 시끄러운 경고 억제)만 담당합니다.
# =============================================================================

import contextlib
import warnings

# 패키지 import 시점에 .env 파일을 로드하여, 어떤 진입점(entry point)에서
# 프로세스를 시작하든 DEFAULT_CONFIG의 환경 변수 오버레이(및 모든 llm_clients
# 사용처)가 사용자의 API 키를 볼 수 있게 합니다.
# find_dotenv(usecwd=True)는 현재 작업 디렉터리(CWD)부터 탐색하므로,
# 설치된 `tradingagents` 콘솔 스크립트가 site-packages에서 위로 올라가는 대신
# 프로젝트의 .env를 집어 들게 됩니다.
# load_dotenv는 기본값이 override=False라서, 호출자가 이미 export한 값을
# 절대 덮어쓰지 않습니다.
try:
    from dotenv import find_dotenv, load_dotenv

    load_dotenv(find_dotenv(usecwd=True))
    load_dotenv(find_dotenv(".env.enterprise", usecwd=True), override=False)
except ImportError:
    pass

# langchain-core 1.3.3은 자신의 __init__에서
# surface_langchain_deprecation_warnings()를 호출하며, 자체 서브클래스 경고
# 카테고리에 대한 default-action 필터를 앞쪽에 끼워 넣습니다. 특정 경고를
# 억제하려면 langchain-core가 자기 필터를 설치한 "이후"에 우리 필터를 설치해야
# 하므로, 먼저 import해 둡니다. 이 패키지는 langgraph를 통해 반드시 함께
# 설치되는 전이 의존성(transitive dependency)입니다.
with contextlib.suppress(ImportError):
    import langchain_core  # noqa: F401

# langgraph-checkpoint 4.0.3은 모듈 로드 시 allowed_objects를 명시하지 않고
# Reviver()를 호출하는데, 이 때문에 인터프리터를 시작할 때마다 langchain-core
# 1.3.3의 시끄러운 PendingDeprecationWarning(예정된 지원 중단 경고)이 뜹니다.
# 수정은 이미 업스트림에 병합되었고(langchain-ai/langgraph#7743, 2026-05-08)
# 다음 langgraph-checkpoint 릴리스에 포함될 예정입니다. 그 버전으로 올린 뒤에는
# 이 블록(그리고 위의 langchain_core 사전 import)을 제거하세요.
warnings.filterwarnings(
    "ignore",
    message=r"The default value of `allowed_objects`.*",
    category=PendingDeprecationWarning,
)
