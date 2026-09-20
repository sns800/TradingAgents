# ============================================================
# [모듈 개요] 매크로 정량 소스 공용 기반 (CollectContext + 파싱 헬퍼)
#
# CONTRACT.md 7장의 소스 모듈 인터페이스가 공유하는 실행 컨텍스트와, SDMX
# CSV/기간 문자열/실수 변환 같은 반복 작업을 한곳에 모았습니다. 소스 모듈은
# 네트워크·저장·오류기록을 직접 하지 않고 전부 이 컨텍스트를 거칩니다.
#
#   SOURCE_NAME = "bis"
#   CADENCE = "daily"
#   def collect(countries, indicators, ctx) -> list[Observation]
#
# 국가별 실패 격리: 한 국가의 HTTP/파싱 실패는 `ctx.record_error(...)`로 기록만
# 하고 다음 국가로 넘어갑니다 (webui/catalog/build_catalog.py의 시장별 실패
# 격리와 같은 방식). 모든 국가가 실패해도 예외를 올리지 않고 빈 리스트를
# 돌려주며, 수집 실패 판정은 collect.py(케이던스 러너)가 errors를 보고 합니다.
#
# [duck typing 계약] registry.py가 아직 없어도 동작해야 하므로 Country/Indicator
# 는 타입이 아니라 "아래 속성을 가진 아무 객체"로 다룹니다. dict를 넘겨도 되게
# `field_of()` 헬퍼가 속성/키를 모두 조회합니다.
#   country.iso    : str  ISO 3166-1 alpha-2 (유로존 "EU")
#   country.iso3   : str  (선택) 세계은행용 3자 코드. 없으면 codes["wb"]
#   country.ccy    : str  ISO 4217 통화 코드 (BIS WS_XRU의 CURRENCY 차원에 필요).
#                         codes["bis_ccy"]가 있으면 그쪽이 우선한다.
#   country.euro   : bool 유로 회원국이면 True (금리·통화량·환율은 EU 참조)
#   country.codes  : dict 소스별 코드
#       {"bis": "KR", "wb": "KOR", "fred_fx": "DEXKOUS",
#        "yahoo_fx": {"symbol": "KRW=X", "invert": False}}
#   indicator.id   : str  CONTRACT 2장 지표 id
#   indicator.unit : str  (선택) CONTRACT 2장 unit
#   indicator.source_entries(name) -> list[dict]   # [{"series": "...",
#                                                     "only": ["US"]}]
#     없으면 indicator.sources(리스트 of dict)에서 name이 같은 항목을 고릅니다.
#     둘 다 없으면 각 소스 모듈의 기본 매핑을 씁니다.
#
# AWS 쓰기 금지: `ctx.raw_saver`가 없으면 원본은 로컬 `./.macro_raw/`에만
# 떨어집니다 (dry-run 스모크용).
# ============================================================
from __future__ import annotations

import csv
import gzip
import io
import logging
import os
import re
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("macro.sources")

# 공개 통계 API 대부분이 익명 User-Agent를 차단하거나 레이트리밋을 강하게
# 적용한다(Yahoo chart API는 UA가 없으면 429). 연락 가능한 URL을 함께 넣는다.
USER_AGENT = "tradingagents-macro/1.0 (+https://stock.happymstn.com)"

# 네트워크 타임아웃(초). 응답이 멈춘 소스 하나가 배치 전체를 잡아두지 않도록
# `ctx.get()`이 기본값으로 항상 적용한다 (tradingagents/dataflows/fred.py와 동일).
DEFAULT_TIMEOUT = 30

# CONTRACT 1장: 유로 회원국(DE/FR/IT)은 아래 지표를 수집하지 않고 EU 값을
# 참조한다. API/프론트가 `euro_area_shared` 플래그로 복제하므로 수집기는 건너뛴다.
EURO_SHARED_INDICATORS = frozenset(
    {"policy_rate", "m2_level", "m2_yoy", "fx_usd", "fx_value_index"}
)

# dry-run(또는 raw_saver 미주입) 시 원본 응답을 떨어뜨리는 로컬 디렉토리.
LOCAL_RAW_DIR = Path(os.environ.get("MACRO_RAW_DIR", ".macro_raw"))

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._+-]+")


def _build_session() -> requests.Session:
    """재시도 2회(backoff 1s, 429/5xx)를 붙인 requests 세션을 만든다."""
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    retry = Retry(
        total=2,
        connect=2,
        read=2,
        status=2,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=8)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


@dataclass
class CollectContext:
    """소스 모듈 실행 컨텍스트 (CONTRACT 7장).

    since       증분 수집 시작일. None이면 각 소스의 기본 시작일을 쓴다.
    dry_run     True면 S3/DynamoDB 쓰기 없이 로컬에만 저장한다.
    no_llm      LLM 소스에서 원문만 수집하고 판정은 생략한다(정량 소스는 무시).
    errors      "{source}:{iso}: {msg}" 문자열 목록 (INGEST 로그로 올라간다).
    log         한 줄 로깅 콜백. 기본 logging.info.
    raw_saver   store.save_raw 주입점. None이면 LOCAL_RAW_DIR에 저장.
    http        requests 세션(재시도 포함). 테스트는 이 필드를 목으로 교체한다.
    llm         ctx.llm.invoke_json(...)을 쓰는 LLM 소스용 핸들.
    extra       소스 간 공유 가방(예: 카탈로그 로더, 사전 계산 결과).
    """

    since: date | None = None
    dry_run: bool = False
    no_llm: bool = False
    errors: list[str] = field(default_factory=list)
    log: Callable[[str], None] = logger.info
    raw_saver: Callable[[str, str, bytes | str | dict], str | None] | None = None
    http: requests.Session = field(default_factory=_build_session)
    llm: Any = None
    extra: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ 원본 저장
    def save_raw(self, source: str, name: str, data: bytes | str | dict) -> str | None:
        """원본 응답을 보존한다. 실패해도 수집을 막지 않는다.

        주입된 `raw_saver`(store.save_raw)가 있으면 그쪽에 위임하고(S3
        `macro/raw/<source>/<YYYY-MM-DD>/<name>.json.gz`, CONTRACT 8장), 없으면
        로컬 `./.macro_raw/<source>/<name>.gz`에 쓴다.
        """
        if self.raw_saver is not None:
            try:
                return self.raw_saver(source, name, data)
            except Exception as exc:  # noqa: BLE001 - 원본 보존 실패는 수집 실패가 아니다
                self.log(f"[{source}] 원본 저장 실패({name}): {exc}")
                return None
        try:
            if isinstance(data, dict):
                import json

                payload = json.dumps(data, ensure_ascii=False).encode()
            elif isinstance(data, str):
                payload = data.encode()
            else:
                payload = data
            out_dir = LOCAL_RAW_DIR / source
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / f"{_SAFE_NAME_RE.sub('_', name)}.gz"
            with gzip.open(path, "wb") as fh:
                fh.write(payload)
            return str(path)
        except Exception as exc:  # noqa: BLE001 - 위와 동일
            self.log(f"[{source}] 로컬 원본 저장 실패({name}): {exc}")
            return None

    # ------------------------------------------------------------------ HTTP
    def get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> requests.Response:
        """GET 요청. raise_for_status는 하지 않고 응답을 그대로 돌려준다.

        상태코드 해석(예: BIS의 "데이터 없음" 404/500)은 소스마다 다르므로
        호출자가 판단한다.
        """
        return self.http.get(url, params=params, headers=headers, timeout=timeout)

    # ------------------------------------------------------------------ 오류
    def record_error(self, source: str, iso: str, msg: str) -> str:
        """국가별 실패를 격리 기록한다(CONTRACT 7장). 예외는 올리지 않는다."""
        entry = f"{source}:{iso}: {msg}"
        self.errors.append(entry)
        self.log(f"[{source}] {iso} 실패: {msg}")
        return entry


# ====================================================================== 헬퍼
def field_of(obj: Any, name: str, default: Any = None) -> Any:
    """속성/딕셔너리 키를 모두 조회한다 (registry 객체·dict 폴백 겸용)."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def country_code(country: Any, key: str, default: Any = None) -> Any:
    """`country.codes[key]`를 안전하게 읽는다. codes가 없으면 동명 속성을 본다."""
    codes = field_of(country, "codes") or {}
    if isinstance(codes, dict) and key in codes:
        return codes[key]
    return field_of(country, key, default)


def source_entries(indicator: Any, source_name: str) -> list[dict[str, Any]]:
    """지표의 소스 항목(`{"series": ..., "only": [...]}`)을 우선순위 순으로 얻는다.

    registry의 Indicator가 `source_entries(name)`을 제공하면 그것을 쓰고, 없으면
    `indicator.sources` 리스트에서 `name`이 일치하는 dict를 직접 고른다. 어느
    쪽도 없으면 빈 리스트 → 소스 모듈이 자체 기본 매핑을 쓴다.
    """
    fn = getattr(indicator, "source_entries", None)
    if callable(fn):
        try:
            return [e for e in (fn(source_name) or []) if isinstance(e, dict)]
        except Exception:  # noqa: BLE001 - registry 구현 차이를 흡수
            pass
    out: list[dict[str, Any]] = []
    for entry in field_of(indicator, "sources") or []:
        if isinstance(entry, dict) and entry.get("name") == source_name:
            out.append(entry)
    return out


def entry_allows(entry: dict[str, Any], iso: str) -> bool:
    """소스 항목의 `only: [ISO...]` 제약을 확인한다 (예: FRED는 US 전용)."""
    only = entry.get("only")
    if not only:
        return True
    return iso.upper() in {str(v).upper() for v in only}


def indicator_unit(indicator: Any, default: str) -> str:
    """indicator.unit이 있으면 그대로, 없으면 모듈 기본 unit을 쓴다."""
    return field_of(indicator, "unit") or default


def find_indicator(indicators: Iterable[Any], indicator_id: str) -> Any | None:
    """요청된 지표 목록에서 id가 일치하는 항목을 찾는다 (없으면 None)."""
    for ind in indicators or []:
        if field_of(ind, "id") == indicator_id:
            return ind
    return None


def iter_countries(
    countries: Sequence[Any],
    indicator: Any,
    source_name: str,
) -> Iterator[Any]:
    """수집 대상 국가를 걸러 준다.

    - `euro: yes` 국가는 EURO_SHARED_INDICATORS(금리·통화량·환율)에서 제외
      (CONTRACT 1장: EU 값을 API/프론트가 참조).
    - 지표의 소스 항목에 `only: [...]`가 있으면 그 목록만 통과.
    """
    indicator_id = field_of(indicator, "id") or ""
    entries = source_entries(indicator, source_name)
    for country in countries or []:
        iso = str(field_of(country, "iso") or "").upper()
        if not iso:
            continue
        if indicator_id in EURO_SHARED_INDICATORS and _is_euro_member(country):
            continue
        if entries and not any(entry_allows(e, iso) for e in entries):
            continue
        yield country


def _is_euro_member(country: Any) -> bool:
    """유로 회원국 판정. registry가 bool/"yes"/"no" 중 무엇을 줘도 받는다."""
    euro = field_of(country, "euro")
    if isinstance(euro, str):
        return euro.strip().lower() in {"yes", "true", "y", "1"}
    return bool(euro)


def sdmx_csv_to_rows(text: str) -> list[dict[str, str]]:
    """SDMX-CSV 응답을 dict 리스트로 바꾼다 (pandas 없이 csv 모듈만 사용).

    BIS SDMX v2의 `format=csv`는 첫 줄이 차원/속성 헤더이고, TITLE 같은 필드에
    쉼표와 따옴표가 섞이므로 수동 split 대신 csv.DictReader가 필요하다. 오류
    응답은 CSV가 아니라 XML(`<?xml ...`)이나 JSON이므로 빈 리스트를 돌려준다.
    """
    if not text:
        return []
    head = text.lstrip()[:5].lower()
    if head.startswith("<?xml") or head.startswith("<"):
        return []
    if head.startswith("{") or head.startswith("["):
        return []
    reader = csv.DictReader(io.StringIO(text))
    rows: list[dict[str, str]] = []
    for row in reader:
        # DictReader는 None 키(열 수 초과)를 만들 수 있어 제거한다.
        rows.append({k: (v or "") for k, v in row.items() if k})
    return rows


def month_key(date_str: str) -> str:
    """`YYYY-MM-DD` / `YYYY-MM` 등에서 월 키 `YYYY-MM`을 뽑는다."""
    s = (date_str or "").strip()
    if len(s) >= 7 and s[4] == "-":
        return s[:7]
    raise ValueError(f"month_key: 알 수 없는 기간 형식 {date_str!r}")


def safe_float(x: Any) -> float | None:
    """빈 문자열·None·"NaN"·천단위 쉼표를 흡수해 float 또는 None을 준다."""
    if x is None:
        return None
    if isinstance(x, (int, float)):
        v = float(x)
        return None if v != v else v  # NaN 제거
    s = str(x).strip().replace(",", "")
    if not s or s.lower() in {"na", "nan", "null", "none", "."}:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return None if v != v else v


def retry_call(fn: Callable[[], Any], tries: int = 2, delay: float = 1.0) -> Any:
    """requests 세션 재시도로 못 잡는 파싱/일시 오류를 한 번 더 시도한다."""
    last: Exception | None = None
    for attempt in range(max(1, tries)):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - 마지막 시도에서 다시 올린다
            last = exc
            if attempt + 1 < max(1, tries):
                time.sleep(delay)
    raise last if last else RuntimeError("retry_call: 알 수 없는 실패")


def last_per_group(pairs: Iterable[tuple[str, float]]) -> list[tuple[str, float]]:
    """(기간, 값) 목록을 기간 오름차순으로 정렬해 그룹별 마지막 값만 남긴다.

    D→M 기말 집계(CONTRACT 6장 `agg: last`)를 소스 모듈에서 자체 계산할 때 쓴다.
    aggregate.py가 준비되면 그쪽으로 옮긴다.
    """
    best: dict[str, tuple[str, float]] = {}
    for period, value in pairs:
        key = month_key(period)
        prev = best.get(key)
        if prev is None or period >= prev[0]:
            best[key] = (period, value)
    return [(k, best[k][1]) for k in sorted(best)]
