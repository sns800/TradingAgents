# ============================================================
# [모듈 개요] 정성 소스 메타데이터 로더 (llm/sources.yaml)
#
# 중앙은행 결정문 페이지, 위키피디아 여론조사 문서 제목·정당 별칭·선거 정보,
# 에너지 정책 원문 URL은 registry.yaml(정량 지표 소유)과 분리해
# webui/macro/llm/sources.yaml 에 둔다 — 정성 계층이 단독으로 갱신하기 위함.
#
# load_sources()는 프로세스 수명 동안 파일을 1회만 읽어 캐시한다.
# ============================================================
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_SOURCES_PATH",
    "central_bank_meta",
    "energy_meta",
    "load_sources",
    "polls_meta",
]

DEFAULT_SOURCES_PATH = Path(__file__).resolve().parent / "sources.yaml"
_CACHE: dict[str, dict[str, Any]] = {}


def load_sources(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """sources.yaml을 읽어 dict로 반환한다 (경로별 캐시).

    반환 구조: {"central_banks": {iso: {...}}, "polls": {iso: {...}},
                "energy_policy": {iso: {"sources": [url, ...]}}}
    """
    p = Path(path) if path is not None else DEFAULT_SOURCES_PATH
    key = str(p)
    if key in _CACHE:
        return _CACHE[key]
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - 워커 이미지에는 PyYAML이 있다
        raise RuntimeError("sources.yaml을 읽으려면 PyYAML이 필요합니다") from exc
    with open(p, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{p}: 최상위가 매핑이 아닙니다")
    for section in ("central_banks", "polls", "energy_policy"):
        data.setdefault(section, {})
        if not isinstance(data[section], dict):
            raise ValueError(f"{p}: {section} 섹션이 매핑이 아닙니다")
    _CACHE[key] = data
    return data


def _section(name: str, iso: str, path: str | os.PathLike[str] | None) -> dict[str, Any]:
    entry = load_sources(path).get(name, {}).get(str(iso).upper())
    return entry if isinstance(entry, dict) else {}


def central_bank_meta(iso: str, path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """국가의 중앙은행 메타. `refer` 키가 있으면 다른 iso(유로존)를 참조하라는 뜻."""
    return _section("central_banks", iso, path)


def polls_meta(iso: str, path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """국가의 여론조사 메타(wiki_page, party_aliases, election, trust, ...)."""
    return _section("polls", iso, path)


def energy_meta(iso: str, path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """국가의 에너지 정책 원문 URL 목록 {"sources": [...]}."""
    return _section("energy_policy", iso, path)
