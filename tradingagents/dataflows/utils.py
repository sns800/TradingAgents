# ============================================================================
# [모듈 개요 - 초보자용]
# 이 파일은 dataflows 패키지 전반에서 공용으로 쓰이는 작은 유틸리티 모음입니다.
# 티커(ticker, 종목 코드)를 파일 경로에 안전하게 쓸 수 있는지 검증하고,
# 데이터프레임 저장, 현재 날짜 조회, 다음 평일 계산 같은 보조 기능을 제공합니다.
# TradingAgents(LLM 멀티 에이전트 주식 트레이딩 프레임워크)에서 캐시 파일 경로
# 생성이나 데이터 저장이 필요한 여러 데이터 수집 모듈이 이 함수들을 가져다 씁니다.
# ============================================================================

import re
from datetime import date, datetime, timedelta
from typing import Annotated

import pandas as pd

SavePathType = Annotated[str, "File path to save data. If None, data is not saved."]

# 스냅샷(snapshot) 데이터 소스 경고의 유예 일수. 펀더멘털 개요·소셜 감성처럼
# "현재 시점" 값만 제공하는 소스는 과거 curr_date로 시점 조회가 불가능하므로,
# curr_date가 오늘로부터 이 일수보다 오래됐으면 백테스트 실행으로 간주해
# 결과 앞에 경고 배너를 붙입니다. 주말·휴일을 넘길 만큼만 관대한 값입니다.
SNAPSHOT_GRACE_DAYS = 3


def is_historical_run(curr_date, *, grace_days: int = SNAPSHOT_GRACE_DAYS) -> bool:
    """``curr_date`` 가 과거(오늘 - grace_days 이전) 날짜인지 여부를 반환한다.

    ``curr_date`` 가 None이거나 파싱할 수 없으면 False를 반환합니다 —
    이 헬퍼는 경고 배너 부착 여부만 결정하며, 데이터 차단에는 쓰이지 않습니다.
    """
    if not curr_date:
        return False
    try:
        target = datetime.strptime(str(curr_date), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return False
    return target < date.today() - timedelta(days=grace_days)


def snapshot_warning_banner(curr_date, data_label: str) -> str:
    """과거 ``curr_date`` 실행에서 '현재 시점 스냅샷' 데이터 앞에 붙일 경고 배너.

    데이터 소스가 시점(point-in-time) 조회를 지원하지 않아 차단 대신 경고로
    완화하는 경우에 사용합니다. 배너는 LLM 컨텍스트에 그대로 들어가므로,
    에이전트가 이 데이터를 curr_date 시점 값으로 오해하지 않게 합니다.
    """
    return (
        f"⚠️ 이 {data_label} 데이터는 현재 시점 스냅샷이며 {curr_date} 시점 값이 "
        "아님 — 백테스트 결과 해석에 주의\n\n"
    )

# 티커에는 영문자, 숫자, 점(.), 대시(-), 밑줄(_), 캐럿(^)
# (지수 심볼 예: ^GSPC), 등호(=)(선물 예: GC=F), 플러스(+)
# (외환/CFD 심볼 예: XAUUSD+)가 올 수 있습니다. 이 문자들은 디렉터리
# 탈출(directory traversal)을 일으키지 못하므로, 경로에 끼워 넣어도
# 값이 상위 디렉터리를 벗어나지 않습니다. 그 외 문자는 모두 거부합니다.
_TICKER_PATH_RE = re.compile(r"^[A-Za-z0-9._\-\^=+]+$")


def safe_ticker_component(value: str, *, max_len: int = 32) -> str:
    """``value``가 파일시스템 경로에 끼워 넣어도 안전한지 검증한다.

    티커는 사용자 CLI 입력이나 LLM 도구 호출(tool call)에서 오는데, 둘 다
    공격자가 제어할 수 있는 콘텐츠(예: 수집한 뉴스에 심어진 프롬프트 주입
    (prompt injection))의 영향을 받을 수 있습니다. 검증 없이는
    ``"../../../etc/foo"`` 같은 값이 ``os.path.join`` / ``Path /`` 로 흘러가
    설정된 캐시·체크포인트·결과 디렉터리를 벗어나게 됩니다.

    허용 패턴과 일치하면 ``value``를 그대로 반환하고, 아니면
    ``ValueError``를 던집니다.
    """
    if not isinstance(value, str) or not value:
        raise ValueError(f"ticker must be a non-empty string, got {value!r}")
    if len(value) > max_len:
        raise ValueError(f"ticker exceeds {max_len} chars: {value!r}")
    if not _TICKER_PATH_RE.fullmatch(value):
        raise ValueError(
            f"ticker contains characters not allowed in a filesystem path: {value!r}"
        )
    # 위 정규식은 '.'을 허용하므로 '.', '..', '...' 같은 값이 통과할 수 있는데,
    # 이런 값은 경로 구성 요소로 쓰이면 상위 디렉터리로 거슬러 올라갑니다.
    # 점으로만 이루어진 값은 모두 거부합니다.
    if set(value) == {"."}:
        raise ValueError(f"ticker cannot consist solely of dots: {value!r}")
    return value


def save_output(data: pd.DataFrame, tag: str, save_path: SavePathType = None) -> None:
    if save_path:
        data.to_csv(save_path, encoding="utf-8")
        print(f"{tag} saved to {save_path}")


def get_current_date():
    return date.today().strftime("%Y-%m-%d")


def decorate_all_methods(decorator):
    def class_decorator(cls):
        for attr_name, attr_value in cls.__dict__.items():
            if callable(attr_value):
                setattr(cls, attr_name, decorator(attr_value))
        return cls

    return class_decorator


def get_next_weekday(date):
    # 주말(토·일)이면 다음 월요일을, 평일이면 그 날짜를 그대로 반환합니다.

    if not isinstance(date, datetime):
        date = datetime.strptime(date, "%Y-%m-%d")

    if date.weekday() >= 5:
        days_to_add = 7 - date.weekday()
        next_weekday = date + timedelta(days=days_to_add)
        return next_weekday
    else:
        return date
