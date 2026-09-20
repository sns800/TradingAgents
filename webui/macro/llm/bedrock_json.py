# ============================================================
# [모듈 개요] Bedrock 구조화 출력 클라이언트 (boto3 bedrock-runtime.converse 직접 호출)
#
# 정성 소스(중앙은행 성명·여론조사 표·에너지 정책)가 LLM에서 **반드시 JSON**을
# 받도록, Converse API의 toolConfig(toolChoice 강제)로 도구 호출을 유도하고
# toolUse.input 딕셔너리를 그대로 반환합니다. 자유 텍스트 파싱을 하지 않으므로
# "JSON 파싱 실패" 경로가 없습니다.
#
# 설계 결정:
#  - langchain-aws를 쓰지 않습니다(워커 이미지 의존성 최소화, boto3만).
#  - 리전 기본 us-east-1, 읽기 타임아웃 300초 — tradingagents/llm_clients 관행.
#  - 모델 기본값은 env MACRO_LLM_MODEL → us.anthropic.claude-haiku-4-5 (CONTRACT 10장).
#  - 호출 전 budget_check(예상 토큰)이 False면 BudgetExceeded를 던져
#    정성 수집만 중단시킵니다(제안서 8장 LLM 비용 상한).
#  - ThrottlingException은 지수 백오프로 최대 3회 재시도.
#  - jsonschema 패키지를 쓰지 않고 required 키 존재·기본 타입만 자체 검증합니다.
#    (의미 검증 — 인용문 존재, 점수 범위, 합계 — 은 각 소스 모듈이 담당)
#
# 주의: 이 모듈은 실제 Bedrock을 호출합니다. 테스트는 client=가짜객체로 주입해
#       네트워크·비용 없이 검증합니다 (tests/test_macro_llm.py).
# ============================================================
from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any

__all__ = [
    "DEFAULT_MODEL_ID",
    "BedrockJson",
    "BudgetExceeded",
    "json_schema_brief",
    "json_schema_energy",
    "json_schema_polls",
    "json_schema_stance",
    "validate_against_schema",
]

DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
DEFAULT_REGION = "us-east-1"
DEFAULT_READ_TIMEOUT = 300
MAX_THROTTLE_RETRIES = 3


class BudgetExceeded(RuntimeError):
    """일일 LLM 토큰 예산을 초과해 호출을 거부했다 (MACRO_LLM_DAILY_TOKEN_BUDGET)."""


# ---------------------------------------------------------------- JSON 스키마
# Converse toolConfig의 inputSchema.json 으로 들어간다. 모델이 이 형태로만
# 응답하도록 toolChoice로 강제하므로, 필드명이 곧 CONTRACT 5장 payload 키다.
json_schema_stance: dict[str, Any] = {
    "type": "object",
    "properties": {
        "stance_score": {
            "type": "number",
            "description": "-2(강한 완화) ~ +2(강한 긴축), 0.5 단위",
        },
        "direction": {"type": "string", "enum": ["hike", "hold", "cut"]},
        "forward_guidance": {"type": "string", "enum": ["tightening", "neutral", "easing"]},
        "rate_after": {"type": "number", "description": "결정 후 정책금리(%), 모르면 생략"},
        "statement_date": {"type": "string", "description": "성명 발표일 YYYY-MM-DD"},
        "meeting_type": {"type": "string", "description": "regular | unscheduled | minutes 등"},
        "title_ko": {"type": "string", "description": "한국어 제목 40자 이내"},
        "summary_ko": {"type": "string", "description": "한국어 요약 3~5문장"},
        "quotes": {
            "type": "array",
            "items": {"type": "string"},
            "description": "원문 그대로의 인용 2~3개 (번역·요약 금지)",
        },
        "confidence": {"type": "number", "description": "0~1"},
    },
    "required": [
        "stance_score",
        "direction",
        "forward_guidance",
        "title_ko",
        "summary_ko",
        "quotes",
        "confidence",
    ],
}

json_schema_polls: dict[str, Any] = {
    "type": "object",
    "properties": {
        "polls": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "pollster": {"type": "string"},
                    "fieldwork_start": {"type": "string", "description": "YYYY-MM-DD"},
                    "fieldwork_end": {"type": "string", "description": "YYYY-MM-DD"},
                    "sample_size": {"type": "integer"},
                    "results": {
                        "type": "object",
                        "additionalProperties": {"type": "number"},
                        "description": "정당명 → 지지율(%)",
                    },
                    "gov_approval": {"type": "number"},
                    "method": {"type": "string"},
                    "row_text": {"type": "string", "description": "이 조사의 표 원문 행 텍스트"},
                },
                "required": ["pollster", "fieldwork_end", "results", "row_text"],
            },
        }
    },
    "required": ["polls"],
}

json_schema_energy: dict[str, Any] = {
    "type": "object",
    "properties": {
        "targets": {
            "type": "array",
            "items": {"type": "string"},
            "description": "수치·연도가 포함된 한국어 목표 문장",
        },
        "recent_changes": {"type": "array", "items": {"type": "string"}},
        "title_ko": {"type": "string"},
        "summary_ko": {"type": "string"},
        "quotes": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
    },
    "required": ["targets", "recent_changes", "title_ko", "summary_ko", "quotes"],
}

json_schema_brief: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title_ko": {"type": "string"},
        "bullets": {
            "type": "array",
            "items": {"type": "string"},
            "description": "한국어 불릿 5~8개, 각 1~2문장",
        },
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"doc_key": {"type": "string"}, "url": {"type": "string"}},
                "required": ["doc_key", "url"],
            },
        },
        "confidence": {"type": "number"},
    },
    "required": ["bullets", "evidence"],
}


# ---------------------------------------------------------------- 최소 검증
_TYPE_MAP: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list, tuple),
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
}


def _type_ok(value: Any, typ: str) -> bool:
    if typ == "null":
        return value is None
    expected = _TYPE_MAP.get(typ)
    if expected is None:
        return True
    if typ in ("number", "integer") and isinstance(value, bool):
        return False  # bool은 int의 하위 타입이지만 숫자로 받지 않는다
    return isinstance(value, expected)


def validate_against_schema(data: Any, schema: dict[str, Any], path: str = "$") -> None:
    """jsonschema 없이 required 키 존재와 기본 타입만 재귀적으로 검사한다.

    실패하면 ValueError. enum·범위·의미 검증은 각 소스 모듈의 책임이다.
    """
    types = schema.get("type")
    if types is not None:
        allowed = [types] if isinstance(types, str) else list(types)
        if not any(_type_ok(data, t) for t in allowed):
            raise ValueError(f"{path}: 타입 불일치 (기대 {allowed}, 실제 {type(data).__name__})")
    if isinstance(data, dict):
        for key in schema.get("required", []):
            if key not in data or data[key] is None:
                raise ValueError(f"{path}.{key}: 필수 키 누락")
        props = schema.get("properties") or {}
        for key, sub in props.items():
            if key in data and data[key] is not None:
                validate_against_schema(data[key], sub, f"{path}.{key}")
        extra = schema.get("additionalProperties")
        if isinstance(extra, dict):
            for key, val in data.items():
                if key not in props and val is not None:
                    validate_against_schema(val, extra, f"{path}.{key}")
    elif isinstance(data, list):
        item = schema.get("items")
        if isinstance(item, dict):
            for i, val in enumerate(data):
                validate_against_schema(val, item, f"{path}[{i}]")


def _is_throttling(exc: BaseException) -> bool:
    """botocore ClientError든 테스트 스텁이든 ThrottlingException 여부를 판정."""
    code = ""
    resp = getattr(exc, "response", None)
    if isinstance(resp, dict):
        code = str((resp.get("Error") or {}).get("Code") or "")
    name = type(exc).__name__
    blob = f"{code} {name}".lower()
    return "throttl" in blob or "toomanyrequests" in blob


class BedrockJson:
    """Bedrock Converse의 도구 호출로 JSON만 받아오는 얇은 래퍼.

    ctx.llm에 이 인스턴스가 주입되고, 소스 모듈은 invoke_json(...)만 쓴다.
    """

    def __init__(
        self,
        *,
        model_id: str | None = None,
        region: str = DEFAULT_REGION,
        client: Any | None = None,
        budget_check: Callable[[int], bool] | None = None,
        on_tokens: Callable[[int], None] | None = None,
        read_timeout: int = DEFAULT_READ_TIMEOUT,
    ) -> None:
        self.model_id = model_id or os.environ.get("MACRO_LLM_MODEL") or DEFAULT_MODEL_ID
        self.region = region
        self.budget_check = budget_check
        self.on_tokens = on_tokens
        self.read_timeout = read_timeout
        self._client = client
        self.tokens_used = 0
        self.calls = 0
        # 테스트에서 백오프 대기를 없애기 위한 훅
        self._sleep: Callable[[float], None] = time.sleep

    @property
    def client(self) -> Any:
        """bedrock-runtime 클라이언트 (첫 호출 때 생성 — import 비용 지연)."""
        if self._client is None:
            import boto3
            from botocore.config import Config

            self._client = boto3.client(
                "bedrock-runtime",
                region_name=self.region,
                config=Config(
                    read_timeout=self.read_timeout,
                    connect_timeout=15,
                    retries={"max_attempts": 0},  # 재시도는 이 클래스가 직접 제어
                ),
            )
        return self._client

    # ------------------------------------------------------------------
    def estimate_tokens(self, user: str, max_tokens: int) -> int:
        """호출 전 예산 판단용 거친 추정치 (입력 문자수/3 + 최대 출력 토큰)."""
        return len(user) // 3 + max_tokens

    def invoke_json(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any],
        tool_name: str = "emit",
        max_tokens: int = 2000,
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        """system/user 프롬프트를 보내고 schema 모양의 dict를 받는다.

        예산 초과면 BudgetExceeded, 도구 호출이 없거나 스키마 필수 키가 빠지면
        ValueError를 던진다. 호출자는 두 예외를 국가 단위로 격리해야 한다.
        """
        est = self.estimate_tokens(user, max_tokens)
        if self.budget_check is not None and not self.budget_check(est):
            raise BudgetExceeded(f"LLM 일일 토큰 예산 초과 (예상 {est} 토큰)")

        params: dict[str, Any] = {
            "modelId": self.model_id,
            "messages": [{"role": "user", "content": [{"text": user}]}],
            "system": [{"text": system}],
            "inferenceConfig": {"maxTokens": max_tokens, "temperature": temperature},
            "toolConfig": {
                "tools": [
                    {
                        "toolSpec": {
                            "name": tool_name,
                            "description": "분석 결과를 이 스키마대로 제출한다.",
                            "inputSchema": {"json": schema},
                        }
                    }
                ],
                "toolChoice": {"tool": {"name": tool_name}},
            },
        }
        resp = self._converse_with_retry(params)
        self.calls += 1
        self._report_tokens(resp)
        data = _extract_tool_input(resp, tool_name)
        validate_against_schema(data, schema)
        return data

    # ------------------------------------------------------------------
    def _converse_with_retry(self, params: dict[str, Any]) -> dict[str, Any]:
        delay = 1.0
        last: BaseException | None = None
        for attempt in range(MAX_THROTTLE_RETRIES + 1):
            try:
                return self.client.converse(**params)
            except Exception as exc:  # noqa: BLE001 - 스로틀만 재시도, 나머지는 재전파
                if not _is_throttling(exc) or attempt >= MAX_THROTTLE_RETRIES:
                    raise
                last = exc
                self._sleep(delay)
                delay *= 2
        raise RuntimeError("converse 재시도 소진") from last

    def _report_tokens(self, resp: dict[str, Any]) -> None:
        usage = resp.get("usage") or {}
        try:
            used = int(usage.get("inputTokens") or 0) + int(usage.get("outputTokens") or 0)
        except (TypeError, ValueError):
            used = 0
        if used <= 0:
            return
        self.tokens_used += used
        if self.on_tokens is not None:
            self.on_tokens(used)


def _extract_tool_input(resp: dict[str, Any], tool_name: str) -> dict[str, Any]:
    """Converse 응답에서 toolUse.input을 꺼낸다 (이름이 달라도 첫 toolUse 허용)."""
    content = ((resp.get("output") or {}).get("message") or {}).get("content") or []
    fallback: dict[str, Any] | None = None
    for block in content:
        if not isinstance(block, dict):
            continue
        tu = block.get("toolUse")
        if not isinstance(tu, dict):
            continue
        payload = tu.get("input")
        if not isinstance(payload, dict):
            continue
        if tu.get("name") == tool_name:
            return payload
        if fallback is None:
            fallback = payload
    if fallback is not None:
        return fallback
    stop = resp.get("stopReason")
    raise ValueError(f"Bedrock 응답에 toolUse가 없다 (stopReason={stop!r})")
