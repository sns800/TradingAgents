# ============================================================
# [모듈 개요] G20 매크로 공용 타입 — CONTRACT.md 3장(관측치)·5장(문서)과 1:1
#
# 수집기·집계·저장·API가 모두 이 dataclass를 기준으로 데이터를 주고받는다.
# 필드나 규칙을 바꾸려면 CONTRACT.md를 먼저 고친다.
# ============================================================
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

FREQS = ("D", "W", "M", "Q", "Y", "E")
FLAGS = (
    "euro_area_shared",
    "estimated",
    "derived",
    "ai_generated",
    "needs_review",
    "fallback_source",
    "partial_period",
)
_PERIOD_RE = {
    "D": re.compile(r"^\d{4}-\d{2}-\d{2}$"),
    "W": re.compile(r"^\d{4}-\d{2}-\d{2}$"),
    "E": re.compile(r"^\d{4}-\d{2}-\d{2}$"),
    "M": re.compile(r"^\d{4}-\d{2}$"),
    "Q": re.compile(r"^\d{4}-Q[1-4]$"),
    "Y": re.compile(r"^\d{4}$"),
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def validate_period(freq: str, period: str) -> None:
    if freq not in FREQS:
        raise ValueError(f"unknown freq {freq!r}")
    if not _PERIOD_RE[freq].match(period):
        raise ValueError(f"period {period!r} does not match freq {freq}")


def to_decimal(v: float | int | None) -> Decimal | None:
    """DynamoDB는 float를 받지 않으므로 Decimal로 변환한다 (소수 6자리 반올림)."""
    if v is None:
        return None
    return Decimal(str(round(float(v), 6)))


@dataclass
class Observation:
    indicator: str
    iso: str
    freq: str
    period: str
    value: float | None
    unit: str
    source: str
    series_id: str
    source_url: str
    method: str
    payload: dict[str, Any] | None = None
    vintage: str = field(default_factory=today_str)
    retrieved_at: str = field(default_factory=now_iso)
    flags: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        validate_period(self.freq, self.period)
        self.iso = self.iso.upper()
        for f in self.flags:
            if f not in FLAGS:
                raise ValueError(f"unknown flag {f!r}")
        if self.value is None and self.payload is None:
            raise ValueError("observation needs value or payload")

    # DynamoDB 키 (CONTRACT 4장)
    @property
    def pk(self) -> str:
        return f"OBS#{self.indicator}#{self.iso}"

    @property
    def sk(self) -> str:
        return f"{self.freq}#{self.period}"

    def to_item(self) -> dict[str, Any]:
        d = asdict(self)
        d["value"] = to_decimal(self.value)
        d["pk"], d["sk"] = self.pk, self.sk
        if d["payload"] is None:
            d.pop("payload")
        else:
            d["payload"] = _decimalize(d["payload"])
        return d

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Doc:
    type: str
    iso: str
    date: str
    id: str
    title_ko: str
    summary_ko: str
    source_url: str
    source_name: str
    payload: dict[str, Any]
    ai_generated: bool = False
    model_id: str | None = None
    confidence: float | None = None
    quotes: list[str] = field(default_factory=list)
    review_status: str = "approved"  # pending | approved | rejected
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    s3_key: str | None = None
    retrieved_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        validate_period("D", self.date)
        self.iso = self.iso.upper()
        if self.ai_generated and not self.quotes:
            raise ValueError("ai_generated doc requires at least one quote")
        if self.ai_generated and self.review_status == "approved" and not self.reviewed_by:
            # AI 산출물은 사람이 승인하기 전까지 pending (사용자 결정: 노출 + 배지)
            self.review_status = "pending"

    @property
    def pk(self) -> str:
        return f"DOC#{self.type}#{self.iso}"

    @property
    def sk(self) -> str:
        return f"{self.date}#{self.id}"

    def to_item(self) -> dict[str, Any]:
        d = asdict(self)
        d["pk"], d["sk"] = self.pk, self.sk
        d["payload"] = _decimalize(d["payload"])
        d["confidence"] = to_decimal(self.confidence)
        return d


def _decimalize(obj: Any) -> Any:
    """중첩 dict/list 안의 float를 Decimal로 바꾼다 (DynamoDB 저장용)."""
    if isinstance(obj, float):
        return to_decimal(obj)
    if isinstance(obj, dict):
        return {k: _decimalize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decimalize(v) for v in obj]
    return obj


def undecimalize(obj: Any) -> Any:
    """DynamoDB에서 읽은 Decimal을 JSON 직렬화 가능한 float/int로 되돌린다."""
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    if isinstance(obj, dict):
        return {k: undecimalize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [undecimalize(v) for v in obj]
    return obj
