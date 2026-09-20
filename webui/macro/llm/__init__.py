# ============================================================
# [모듈 개요] G20 매크로 정성 데이터·LLM 계층 (webui/macro/llm)
#
# CONTRACT.md 7장의 소스 모듈 인터페이스를 따르는 LLM 기반 수집기 묶음입니다.
# 정량 소스(sources/*)와 달리 원문(HTML)을 받아 Bedrock으로 구조화하고,
# 반드시 **원문 인용(quotes)**을 함께 저장해 사람이 검증할 수 있게 합니다.
#
#  - bedrock_json : Converse toolConfig로 JSON 강제 (BedrockJson, 스키마 상수)
#  - meta         : sources.yaml 로더 (국가별 정성 소스 메타)
#  - textutil     : HTML→텍스트, 표/링크 파서, 날짜 범위 파서, 인용 검증
#  - cb_statements: 중앙은행 결정문 → cb_stance 문서/관측치 (daily)
#  - polls_wiki   : 위키피디아 여론조사 표 → poll/election 문서·party_support (weekly)
#  - energy_policy: 에너지 정책 원문 → energy_policy 문서 (weekly, 해시 변경 감지)
#  - weekly_brief : 주간 요약 브리프 문서 (iso="G20")
#
# ctx.no_llm 이면 모든 모듈은 원문만 S3에 저장하고 문서를 만들지 않습니다.
# ============================================================
from __future__ import annotations

from .bedrock_json import (
    DEFAULT_MODEL_ID,
    BedrockJson,
    BudgetExceeded,
    json_schema_brief,
    json_schema_energy,
    json_schema_polls,
    json_schema_stance,
)
from .meta import central_bank_meta, energy_meta, load_sources, polls_meta

__all__ = [
    "DEFAULT_MODEL_ID",
    "BedrockJson",
    "BudgetExceeded",
    "central_bank_meta",
    "energy_meta",
    "json_schema_brief",
    "json_schema_energy",
    "json_schema_polls",
    "json_schema_stance",
    "load_sources",
    "polls_meta",
]
