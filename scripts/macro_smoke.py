#!/usr/bin/env python3
# ============================================================
# [모듈 개요] 매크로 정량 소스 스모크 스크립트 (실제 공개 API 호출)
#
# 소스 모듈 하나를 실제로 호출해 관측치 수·국가별 최신 값·오류를 표로 찍는다.
# 유닛 테스트는 픽스처만 쓰므로(네트워크 없음), "지금 API가 살아 있는지"는 이
# 스크립트로 확인한다.
#
# 실행 예:
#   python scripts/macro_smoke.py --source bis --countries KR,US,EU --since 2026-06-01
#   python scripts/macro_smoke.py --source worldbank --countries KR,US,EU,JP
#   python scripts/macro_smoke.py --source yahoo --countries KR,EU --since 2026-09-01
#   python scripts/macro_smoke.py --source fred                  # 키 없으면 스킵 1줄
#   python scripts/macro_smoke.py --source bis --save-raw        # 원본을 로컬에 보존
#
# 항상 ctx.dry_run=True 이고 AWS 쓰기는 하지 않는다. `--save-raw` 없이는 원본도
# 남기지 않는다(raw_saver를 no-op으로 갈아끼움).
#
# registry.py가 준비되면 `macro.registry.load_registry()`의 국가·지표를 쓰고,
# 아직 없으면 이 파일의 최소 폴백(FALLBACK_COUNTRIES/INDICATORS)을 쓴다.
# ============================================================
from __future__ import annotations

import argparse
import importlib
import logging
import socket
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WEBUI = REPO_ROOT / "webui"
if str(WEBUI) not in sys.path:
    sys.path.insert(0, str(WEBUI))

# yfinance 등 일부 라이브러리의 내부 HTTP 호출에는 타임아웃이 없어, 응답 없는
# 소켓 읽기에서 스모크가 무한 대기할 수 있다. 전역 소켓 기본 타임아웃을 걸면
# 그런 호출이 예외로 바뀌어 소스별 실패 격리가 다음 항목으로 진행시킨다
# (webui/catalog/build_catalog.py에서 검증된 패턴). 명시적 timeout이 있는
# requests 호출에는 영향이 없다.
socket.setdefaulttimeout(120)

from macro.sources.base import CollectContext  # noqa: E402 - sys.path 설정 후 임포트

SOURCE_MODULES = {
    "bis": "macro.sources.bis",
    "worldbank": "macro.sources.worldbank",
    "yahoo": "macro.sources.yahoo_fx",
    "fred": "macro.sources.fred",
}

# 소스별로 요청할 지표 (레지스트리가 없을 때)
SOURCE_INDICATORS = {
    "bis": ["policy_rate", "fx_usd", "house_price_index"],
    "worldbank": [
        "gdp_usd",
        "gdp_growth",
        "gni_pc",
        "gov_debt_gdp",
        "mil_expenditure_share",
        "va_manuf",
        "exports_gdp",
    ],
    "yahoo": ["fx_usd", "dxy"],
    "fred": ["policy_rate", "m2_level", "cpi_index", "ppi_index", "house_price_index", "dxy"],
}

# CONTRACT 1장 축약본 (iso, iso3, ccy, euro) — registry.py가 없을 때만 쓴다.
FALLBACK_COUNTRIES: dict[str, tuple[str, str, bool]] = {
    "KR": ("KOR", "KRW", False),
    "US": ("USA", "USD", False),
    "JP": ("JPN", "JPY", False),
    "CN": ("CHN", "CNY", False),
    "EU": ("EMU", "EUR", False),
    "DE": ("DEU", "EUR", True),
    "FR": ("FRA", "EUR", True),
    "IT": ("ITA", "EUR", True),
    "GB": ("GBR", "GBP", False),
    "CA": ("CAN", "CAD", False),
    "AU": ("AUS", "AUD", False),
    "IN": ("IND", "INR", False),
    "ID": ("IDN", "IDR", False),
    "BR": ("BRA", "BRL", False),
    "MX": ("MEX", "MXN", False),
    "AR": ("ARG", "ARS", False),
    "TR": ("TUR", "TRY", False),
    "SA": ("SAU", "SAR", False),
    "ZA": ("ZAF", "ZAR", False),
    "RU": ("RUS", "RUB", False),
}
# 유로존 BIS 코드는 XM (CONTRACT 1장)
BIS_CODE = {"EU": "XM"}

# 지표 기본 unit (CONTRACT 2장) — 폴백 지표 객체에 채워 넣는다.
FALLBACK_UNITS = {
    "policy_rate": "%",
    "fx_usd": "lcu_per_usd",
    "house_price_index": "index",
    "cpi_index": "index",
    "ppi_index": "index",
    "m2_level": "usd_bn",
    "dxy": "index",
    "gdp_usd": "usd",
    "gdp_growth": "%",
    "gni_pc": "usd",
    "gov_debt_gdp": "pct_gdp",
    "mil_expenditure_share": "pct_share",
    "va_manuf": "pct_gdp",
    "exports_gdp": "pct_gdp",
}


class _Country:
    """registry.Country 최소 스텁 (base.py의 duck typing 계약만 충족)."""

    def __init__(self, iso: str) -> None:
        iso3, ccy, euro = FALLBACK_COUNTRIES[iso]
        self.iso = iso
        self.iso3 = iso3
        self.ccy = ccy
        self.euro = euro
        self.codes = {"bis": BIS_CODE.get(iso, iso), "wb": iso3}

    def __repr__(self) -> str:  # pragma: no cover - 디버깅 편의
        return f"<Country {self.iso}>"


class _Indicator:
    """registry.Indicator 최소 스텁 (source_entries는 빈 목록 → 모듈 기본 매핑)."""

    def __init__(self, indicator_id: str) -> None:
        self.id = indicator_id
        self.unit = FALLBACK_UNITS.get(indicator_id)
        self.sources: list[dict] = []

    def source_entries(self, name: str) -> list[dict]:
        return [e for e in self.sources if e.get("name") == name]

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Indicator {self.id}>"


def _load_registry(isos: list[str], indicator_ids: list[str]) -> tuple[list, list, str]:
    """registry.py가 있으면 그쪽 객체를, 없으면 스텁을 돌려준다."""
    try:
        registry = importlib.import_module("macro.registry")
        reg = registry.load_registry()
        countries = [c for c in reg.countries if getattr(c, "iso", None) in isos]
        indicators = [i for i in reg.indicators if getattr(i, "id", None) in indicator_ids]
        if countries and indicators:
            return countries, indicators, "macro.registry"
    except Exception:  # noqa: BLE001 - registry.py는 다른 에이전트가 작업 중이라 없을 수 있다
        pass
    return (
        [_Country(iso) for iso in isos],
        [_Indicator(i) for i in indicator_ids],
        "폴백 스텁(registry.py 없음)",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="매크로 정량 소스 스모크 (실제 API 호출)")
    parser.add_argument("--source", required=True, choices=sorted(SOURCE_MODULES))
    parser.add_argument(
        "--countries",
        default="KR,US,EU",
        help="쉼표 구분 ISO2 목록. 'all'이면 CONTRACT 1장 20개국 전부",
    )
    parser.add_argument("--since", default=None, help="증분 시작일 YYYY-MM-DD")
    parser.add_argument("--indicators", default=None, help="쉼표 구분 지표 id (기본: 소스별 전체)")
    parser.add_argument("--save-raw", action="store_true", help="원본 응답을 ./.macro_raw/에 저장")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.countries.strip().lower() == "all":
        isos = list(FALLBACK_COUNTRIES)
    else:
        isos = [c.strip().upper() for c in args.countries.split(",") if c.strip()]
    unknown = [i for i in isos if i not in FALLBACK_COUNTRIES]
    if unknown:
        print(f"알 수 없는 국가 코드: {', '.join(unknown)}", file=sys.stderr)
        return 2

    indicator_ids = (
        [i.strip() for i in args.indicators.split(",") if i.strip()]
        if args.indicators
        else SOURCE_INDICATORS[args.source]
    )
    since = date.fromisoformat(args.since) if args.since else None

    countries, indicators, reg_note = _load_registry(isos, indicator_ids)
    module = importlib.import_module(SOURCE_MODULES[args.source])

    ctx = CollectContext(since=since, dry_run=True)
    if not args.save_raw:
        # 원본 보존은 --save-raw 때만. no-op raw_saver로 로컬 쓰기까지 막는다.
        ctx.raw_saver = lambda source, name, data: None

    print(f"소스={module.SOURCE_NAME} 케이던스={module.CADENCE} 레지스트리={reg_note}")
    print(f"국가={','.join(isos)} 지표={','.join(indicator_ids)} since={since or '(기본)'}")
    print("-" * 78)

    observations = module.collect(countries, indicators, ctx)
    _print_report(observations, ctx)
    return 0 if observations else 1


def _print_report(observations: list, ctx: CollectContext) -> None:
    print(f"관측치 {len(observations)}건")
    if observations:
        by_key: dict[tuple[str, str, str], list] = {}
        for obs in observations:
            by_key.setdefault((obs.indicator, obs.freq, obs.iso), []).append(obs)
        print(f"{'indicator':>22} {'f':>2} {'iso':>4} {'n':>6} {'latest':>12} {'value':>18} unit")
        for key in sorted(by_key):
            rows = sorted(by_key[key], key=lambda o: o.period)
            last = rows[-1]
            value = "(payload)" if last.value is None else f"{last.value:,.4f}"
            print(
                f"{key[0]:>22} {key[1]:>2} {key[2]:>4} {len(rows):>6} "
                f"{last.period:>12} {value:>18} {last.unit}"
            )
        covered = sorted({o.iso for o in observations})
        print(f"커버리지 {len(covered)}개국: {','.join(covered)}")
    if ctx.errors:
        print(f"오류 {len(ctx.errors)}건")
        for err in ctx.errors:
            print(f"  - {err}")
    else:
        print("오류 없음")


if __name__ == "__main__":
    raise SystemExit(main())
