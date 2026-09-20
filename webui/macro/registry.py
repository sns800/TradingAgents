# ============================================================
# [모듈 개요] G20 매크로 지표 레지스트리 로더 — CONTRACT.md 1·2장의 코드 표현
#
# registry.yaml(국가 20개 + 지표 38개)을 읽어 Country/Indicator/Registry 객체로 만들고,
# 수집기·집계·저장·API가 "무엇을 어디서 받아 어떻게 저장하는지"를 이 객체만 보고 판단한다.
# 국가 목록·소스 코드·시리즈 문자열을 다른 모듈에 하드코딩하지 않는다.
#
# 주요 기능
#  - load_registry(path=None) -> Registry      : YAML 로드 (기본: 이 파일과 같은 디렉터리)
#  - validate(registry)                        : CONTRACT 위반을 ValueError로 한 번에 보고
#  - Registry.countries_for_source(name, ind)  : 소스별 수집 대상 국가 (유로 참조 규칙 반영)
#  - Registry.to_meta_items()                  : DynamoDB `SERIES#<indicator>/META` 항목
#
# 유로 참조 규칙 (CONTRACT 1장): euro=true 국가(DE/FR/IT)의 policy_rate·m2_*·fx_* 는
# 수집하지 않고 API/프론트가 EU 값을 참조한다(flags: [euro_area_shared]).
# ============================================================
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from macro.schema import FREQS, now_iso

# CONTRACT 2장 "지표 id 고정 목록" — 순서까지 문서와 동일하게 유지한다.
INDICATOR_IDS: tuple[str, ...] = (
    "policy_rate",
    "fx_usd",
    "fx_value_index",
    "m2_level",
    "m2_yoy",
    "cpi_index",
    "cpi_yoy",
    "core_cpi_yoy",
    "ppi_index",
    "ppi_yoy",
    "house_price_index",
    "house_price_yoy",
    "gdp_usd",
    "gdp_growth",
    "gni_usd",
    "gni_pc",
    "gov_expense_gdp",
    "gov_revenue_gdp",
    "gov_debt_gdp",
    "mil_gdp",
    "mil_expenditure_share",
    "mil_usd",
    "va_agri",
    "va_industry",
    "va_manuf",
    "va_services",
    "exports_gdp",
    "exports_top_hs2",
    "elec_mix",
    "energy_import_dep",
    "fuel_dep_oil",
    "fuel_dep_gas",
    "fuel_dep_coal",
    "party_support",
    "gov_approval",
    "cb_stance",
    "top_companies",
    "dxy",
)

# CONTRACT 2장 열거값
UNITS: tuple[str, ...] = (
    "%",
    "index",
    "usd",
    "usd_bn",
    "lcu_bn",  # 자국통화 10억 단위 잔액 (m2_level — USD 환산 없음, 국가 간 비교 금지)
    "lcu_per_usd",
    "pct_gdp",
    "pct_share",
    "score",
)
CATEGORIES: tuple[str, ...] = (
    "politics",
    "monetary",
    "fx",
    "structure",
    "energy",
    "fiscal",
    "market",
)
AGGS: tuple[str, ...] = ("last", "mean", "sum", "none")

# CONTRACT 3장 `source` 값 (소스 모듈 이름)
SOURCE_NAMES: tuple[str, ...] = (
    "bis",
    "oecd",
    "worldbank",
    "imf",
    "fred",
    "ember",
    "owid",
    "wits",
    "yahoo",
    "catalog",
    "companies",  # 모듈·INGEST 식별자. Observation.source는 catalog 또는 yahoo
    "wiki_polls",
    "cb_statements",
    "derived",
)

# 국가별 소스 코드 키 (registry.yaml의 codes.*)
CODE_KEYS: tuple[str, ...] = ("bis", "oecd", "wb", "imf", "yahoo_fx", "fred_fx", "ember", "owid")

# 빈도 순위: 숫자가 크면 더 낮은(거친) 빈도. store_freqs는 native 이상 순위만 허용.
FREQ_RANK: dict[str, int] = {"D": 0, "E": 0, "W": 1, "M": 2, "Q": 3, "Y": 4}

_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")

_COUNTRY_FIELDS = ("iso", "iso3", "name_ko", "name_en", "ccy", "groups", "euro", "codes")
_INDICATOR_FIELDS = (
    "id",
    "name_ko",
    "unit",
    "category",
    "native_freq",
    "store_freqs",
    "agg",
    "decimals",
    "sources",
    "yoy_from",
    "derived_from",
    "higher_is",
    "composite",
)
# API `GET /api/macro/meta`가 쓰는 지표 필드 (CONTRACT 9장)
_META_FIELDS = (
    "id",
    "name_ko",
    "unit",
    "category",
    "native_freq",
    "store_freqs",
    "agg",
    "decimals",
)


def is_euro_shared(indicator_id: str) -> bool:
    """유로존 단일값 지표인가 (CONTRACT 1장: policy_rate, m2_*, fx_*)."""
    return (
        indicator_id == "policy_rate"
        or indicator_id.startswith("m2_")
        or indicator_id.startswith("fx_")
    )


@dataclass
class Country:
    """CONTRACT 1장의 국가 1건 + 소스별 코드."""

    iso: str
    iso3: str
    name_ko: str
    ccy: str
    groups: list[str]
    euro: bool
    codes: dict[str, Any]
    name_en: str = ""

    def code(self, source: str) -> str | None:
        """소스별 국가 코드. dict 코드(yahoo_fx/ember/owid)는 대표 문자열을 돌려준다."""
        raw = self.codes.get(source)
        if isinstance(raw, dict):
            raw = raw.get("symbol") or raw.get("name")
        if raw is None:
            return None
        return str(raw)

    @property
    def yahoo_fx(self) -> tuple[str, bool] | None:
        """(심볼, invert). invert=True면 1/x 해야 현지통화/USD가 된다. US는 None."""
        raw = self.codes.get("yahoo_fx")
        if not isinstance(raw, dict) or not raw.get("symbol"):
            return None
        return str(raw["symbol"]), bool(raw.get("invert", False))

    def in_group(self, group: str) -> bool:
        return group in self.groups

    def to_meta(self) -> dict[str, Any]:
        """API `/meta`·DynamoDB `SERIES#__countries__` 용 dict."""
        return {
            "iso": self.iso,
            "iso3": self.iso3,
            "name_ko": self.name_ko,
            "name_en": self.name_en,
            "ccy": self.ccy,
            "groups": list(self.groups),
            "euro": self.euro,
            "codes": dict(self.codes),
        }


@dataclass
class Indicator:
    """CONTRACT 2장의 지표 1건."""

    id: str
    name_ko: str
    unit: str
    category: str
    native_freq: str
    store_freqs: list[str]
    agg: str
    decimals: int
    sources: list[dict[str, Any]] = field(default_factory=list)
    yoy_from: str | None = None
    derived_from: str | None = None
    higher_is: str = "neutral"
    composite: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_composite(self) -> bool:
        """value 대신 payload에 저장하는 복합값 지표인가 (CONTRACT 3장)."""
        return bool(self.composite)

    @property
    def is_derived(self) -> bool:
        """다른 지표에서 계산되는 파생 지표인가 (1순위 소스가 derived면 파생)."""
        if self.yoy_from or self.derived_from:
            return True
        entries = self.sorted_sources()
        return bool(entries) and entries[0].get("name") == "derived"

    def sorted_sources(self) -> list[dict[str, Any]]:
        return sorted(self.sources, key=lambda e: e.get("priority", 99))

    def source_names(self) -> list[str]:
        """우선순위 순 소스 이름 목록(중복 제거)."""
        seen: list[str] = []
        for e in self.sorted_sources():
            for name in (e.get("name"), e.get("via")):
                if name and name not in seen:
                    seen.append(name)
        return seen

    def source_entries(self, name: str) -> list[dict[str, Any]]:
        """이름(또는 파생 항목의 `via`)이 일치하는 소스 항목을 우선순위 순으로."""
        return [
            e
            for e in self.sorted_sources()
            if e.get("name") == name or e.get("via") == name
        ]

    def to_meta(self) -> dict[str, Any]:
        d = {
            "id": self.id,
            "name_ko": self.name_ko,
            "unit": self.unit,
            "category": self.category,
            "native_freq": self.native_freq,
            "store_freqs": list(self.store_freqs),
            "agg": self.agg,
            "decimals": self.decimals,
            "higher_is": self.higher_is,
            "composite": self.composite,
            "yoy_from": self.yoy_from,
            "derived_from": self.derived_from,
            "sources": [dict(e) for e in self.sorted_sources()],
        }
        return d

    def to_api_meta(self) -> dict[str, Any]:
        """API `/api/macro/meta`의 indicators 항목 (CONTRACT 9장)."""
        return {k: v for k, v in self.to_meta().items() if k in _META_FIELDS}


@dataclass
class Registry:
    """registry.yaml 전체."""

    countries: list[Country]
    indicators: list[Indicator]
    path: Path | None = None

    def __post_init__(self) -> None:
        self._by_iso = {c.iso: c for c in self.countries}
        self._by_id = {i.id: i for i in self.indicators}

    # ----- 조회 -----
    def country(self, iso: str) -> Country:
        try:
            return self._by_iso[iso.upper()]
        except KeyError:
            raise ValueError(f"등록되지 않은 국가 코드: {iso!r}") from None

    def indicator(self, indicator_id: str) -> Indicator:
        try:
            return self._by_id[indicator_id]
        except KeyError:
            raise ValueError(f"등록되지 않은 지표 id: {indicator_id!r}") from None

    def has_country(self, iso: str) -> bool:
        return iso.upper() in self._by_iso

    def has_indicator(self, indicator_id: str) -> bool:
        return indicator_id in self._by_id

    @property
    def iso_codes(self) -> list[str]:
        return [c.iso for c in self.countries]

    @property
    def indicator_ids(self) -> list[str]:
        return [i.id for i in self.indicators]

    def by_group(self, group: str) -> list[Country]:
        """그룹(G20/G7/EU/BRICS/ASIA) 소속 국가."""
        return [c for c in self.countries if c.in_group(group)]

    def euro_members(self) -> list[Country]:
        """유로 참조 국가(DE/FR/IT)."""
        return [c for c in self.countries if c.euro]

    def indicators_for_source(self, name: str) -> list[Indicator]:
        """해당 소스가 1순위든 폴백이든 담당하는 지표 목록(레지스트리 순서).

        파생 항목의 `via`(원자료 제공 소스)도 함께 본다 — `indicators_for_source("owid")`는
        `sources: [{name: derived, via: owid}]`로 선언된 fuel_dep_* 도 돌려준다.
        """
        return [i for i in self.indicators if i.source_entries(name)]

    def countries_for_source(self, name: str, indicator: str | None = None) -> list[Country]:
        """소스가 실제로 수집할 국가 목록.

        국가 선별 규칙
        - `only`가 있는 소스 항목은 해당 iso만 대상.
        - 시리즈/키 문자열에 `{bis}`·`{oecd}`·`{yahoo_fx}`처럼 코드 자리표시자가 있으면
          그 코드를 가진 국가만 대상 (예: fred 환율 폴백은 H.10 시리즈가 있는 국가만).

        유로 참조 규칙 (CONTRACT 1장)
        - `indicator`를 지정하면: 그 지표가 유로 공통(policy_rate/m2_*/fx_*)일 때 euro 국가 제외.
        - 지정하지 않으면: 그 소스가 유로 공통 지표를 **원천 빈도 그대로** 제공하는
          실질 제공자일 때 euro 국가 제외 (BIS 정책금리·환율, IMF 통화량, yahoo/fred 환율).
          World Bank처럼 유로 공통 지표를 연간 폴백으로만 제공하는 소스는 국가별 지표
          (GDP·부가가치 등)를 받아야 하므로 euro 국가를 유지한다.
        - 유로 회원국 고유 데이터(예: BIS 독일 집값)는 지표를 명시해서 받는다:
          `countries_for_source("bis", "house_price_index")`.
        """
        if indicator:
            inds = [self.indicator(indicator)]
            skip_euro = is_euro_shared(indicator)
        else:
            inds = self.indicators_for_source(name)
            skip_euro = None  # 국가별로 판단
        out: list[Country] = []
        for c in self.countries:
            if c.euro:
                drop = skip_euro if skip_euro is not None else self._is_native_euro_provider(name, c)
                if drop:
                    continue
            if any(self._entry_covers(name, ind, c) for ind in inds):
                out.append(c)
        return out

    def _is_native_euro_provider(self, name: str, country: Country) -> bool:
        """이 소스가 해당 국가에 대해 유로 공통 지표를 원천 빈도로 제공하는가."""
        for ind in self.indicators:
            if not is_euro_shared(ind.id):
                continue
            for entry in ind.source_entries(name):
                if not self._entry_matches_country(entry, country):
                    continue
                if str(entry.get("freq") or ind.native_freq) == ind.native_freq:
                    return True
        return False

    @staticmethod
    def _entry_matches_country(entry: dict[str, Any], country: Country) -> bool:
        only = entry.get("only")
        if only and country.iso not in [str(x).upper() for x in only]:
            return False
        template = f"{entry.get('series', '')} {entry.get('key', '')}"
        needed = {m for m in _PLACEHOLDER_RE.findall(template) if m in CODE_KEYS}
        return all(country.code(k) for k in needed)

    def _entry_covers(self, name: str, ind: Indicator, country: Country) -> bool:
        return any(
            self._entry_matches_country(entry, country) for entry in ind.source_entries(name)
        )

    # ----- DynamoDB 메타 -----
    def to_meta_items(self) -> list[dict[str, Any]]:
        """CONTRACT 4장 `SERIES#<indicator>/META` + `SERIES#__countries__/META` 항목.

        API가 YAML 없이 DynamoDB만으로 메타를 읽을 수 있게 레지스트리를 그대로 옮긴다.
        """
        stamp = now_iso()
        items: list[dict[str, Any]] = []
        for ind in self.indicators:
            item = {"pk": f"SERIES#{ind.id}", "sk": "META", "updated_at": stamp}
            item.update(ind.to_meta())
            items.append(item)
        items.append(
            {
                "pk": "SERIES#__countries__",
                "sk": "META",
                "updated_at": stamp,
                "countries": [c.to_meta() for c in self.countries],
            }
        )
        return items


def default_registry_path() -> Path:
    return Path(__file__).resolve().parent / "registry.yaml"


def load_registry(path: str | Path | None = None) -> Registry:
    """registry.yaml을 읽어 Registry를 만든다(검증은 validate()를 따로 호출)."""
    p = Path(path) if path else default_registry_path()
    if not p.exists():
        raise ValueError(f"레지스트리 파일을 찾을 수 없다: {p}")
    with p.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"레지스트리 최상위는 매핑이어야 한다: {p}")

    countries = [_country_from_dict(d, p) for d in raw.get("countries") or []]
    indicators = [_indicator_from_dict(d, p) for d in raw.get("indicators") or []]
    return Registry(countries=countries, indicators=indicators, path=p)


def _country_from_dict(d: dict[str, Any], path: Path) -> Country:
    if not isinstance(d, dict) or not d.get("iso"):
        raise ValueError(f"국가 항목에 iso가 없다 ({path}): {d!r}")
    unknown = set(d) - set(_COUNTRY_FIELDS)
    if unknown:
        raise ValueError(f"국가 {d['iso']}에 알 수 없는 필드: {sorted(unknown)}")
    return Country(
        iso=str(d["iso"]).upper(),
        iso3=str(d.get("iso3") or ""),
        name_ko=str(d.get("name_ko") or ""),
        name_en=str(d.get("name_en") or ""),
        ccy=str(d.get("ccy") or ""),
        groups=[str(g) for g in d.get("groups") or []],
        euro=bool(d.get("euro") or False),
        codes=dict(d.get("codes") or {}),
    )


def _indicator_from_dict(d: dict[str, Any], path: Path) -> Indicator:
    if not isinstance(d, dict) or not d.get("id"):
        raise ValueError(f"지표 항목에 id가 없다 ({path}): {d!r}")
    extra = {k: v for k, v in d.items() if k not in _INDICATOR_FIELDS}
    return Indicator(
        id=str(d["id"]),
        name_ko=str(d.get("name_ko") or ""),
        unit=str(d.get("unit") or ""),
        category=str(d.get("category") or ""),
        native_freq=str(d.get("native_freq") or ""),
        store_freqs=[str(f) for f in d.get("store_freqs") or []],
        agg=str(d.get("agg") or "none"),
        decimals=int(d.get("decimals") if d.get("decimals") is not None else 2),
        sources=[dict(e) for e in d.get("sources") or []],
        yoy_from=d.get("yoy_from") or None,
        derived_from=d.get("derived_from") or None,
        higher_is=str(d.get("higher_is") or "neutral"),
        composite=bool(d.get("composite") or False),
        extra=extra,
    )


def validate(registry: Registry) -> None:
    """CONTRACT 1·2장 위반을 모두 모아 ValueError 하나로 보고한다."""
    errs: list[str] = []
    errs += _validate_countries(registry)
    errs += _validate_indicators(registry)
    if errs:
        raise ValueError("레지스트리 검증 실패:\n- " + "\n- ".join(errs))


def _validate_countries(registry: Registry) -> list[str]:
    errs: list[str] = []
    countries = registry.countries
    if len(countries) != 20:
        errs.append(f"국가 수는 20이어야 한다 (현재 {len(countries)})")
    seen: set[str] = set()
    for c in countries:
        if c.iso in seen:
            errs.append(f"국가 iso 중복: {c.iso}")
        seen.add(c.iso)
        if len(c.iso) != 2:
            errs.append(f"iso는 2자리여야 한다: {c.iso!r}")
        if not c.iso3:
            errs.append(f"{c.iso}: iso3가 비었다")
        if not c.name_ko:
            errs.append(f"{c.iso}: name_ko가 비었다")
        if not c.ccy:
            errs.append(f"{c.iso}: ccy가 비었다")
        if not c.groups:
            errs.append(f"{c.iso}: groups가 비었다")
        if not isinstance(c.euro, bool):
            errs.append(f"{c.iso}: euro는 bool이어야 한다")
        unknown = sorted(set(c.codes) - set(CODE_KEYS))
        if unknown:
            errs.append(f"{c.iso}: 알 수 없는 codes 키 {unknown}")
        for key in ("bis", "oecd", "wb", "imf"):
            if not c.codes.get(key):
                errs.append(f"{c.iso}: codes.{key}가 없다")
        raw_fx = c.codes.get("yahoo_fx")
        if raw_fx is not None and (
            not isinstance(raw_fx, dict) or "symbol" not in raw_fx or "invert" not in raw_fx
        ):
            errs.append(f"{c.iso}: codes.yahoo_fx는 {{symbol, invert}} 또는 null이어야 한다")
    return errs


def _validate_indicators(registry: Registry) -> list[str]:
    errs: list[str] = []
    ids = registry.indicator_ids
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        errs.append(f"지표 id 중복: {dupes}")
    missing = [i for i in INDICATOR_IDS if i not in set(ids)]
    extra = [i for i in ids if i not in set(INDICATOR_IDS)]
    if missing or extra:
        errs.append(
            "지표 id 목록이 CONTRACT 2장과 다르다 "
            f"(누락 {missing or '없음'} / 초과 {extra or '없음'})"
        )

    for ind in registry.indicators:
        if ind.unit not in UNITS:
            errs.append(f"{ind.id}: unit {ind.unit!r}은 열거값이 아니다 {list(UNITS)}")
        if ind.category not in CATEGORIES:
            errs.append(f"{ind.id}: category {ind.category!r}은 열거값이 아니다 {list(CATEGORIES)}")
        if ind.native_freq not in FREQS:
            errs.append(f"{ind.id}: native_freq {ind.native_freq!r}은 열거값이 아니다 {list(FREQS)}")
        if ind.agg not in AGGS:
            errs.append(f"{ind.id}: agg {ind.agg!r}은 열거값이 아니다 {list(AGGS)}")
        if ind.higher_is != "neutral":
            errs.append(f"{ind.id}: higher_is는 neutral만 허용한다 (CONTRACT 2장)")
        if not isinstance(ind.decimals, int) or not 0 <= ind.decimals <= 6:
            errs.append(f"{ind.id}: decimals는 0~6 정수여야 한다 (현재 {ind.decimals!r})")
        if not ind.store_freqs:
            errs.append(f"{ind.id}: store_freqs가 비었다")
        native_rank = FREQ_RANK.get(ind.native_freq)
        for f in ind.store_freqs:
            if f not in FREQS:
                errs.append(f"{ind.id}: store_freqs의 {f!r}은 열거값이 아니다")
            elif native_rank is not None and FREQ_RANK[f] < native_rank:
                errs.append(
                    f"{ind.id}: store_freqs {f}는 native_freq {ind.native_freq}보다 고빈도다"
                )
        if ind.native_freq not in ind.store_freqs:
            errs.append(f"{ind.id}: store_freqs에 native_freq {ind.native_freq}가 없다")
        for fname, ref in (("yoy_from", ind.yoy_from), ("derived_from", ind.derived_from)):
            if ref and not registry.has_indicator(str(ref)):
                errs.append(f"{ind.id}: {fname}={ref!r}는 등록되지 않은 지표 id다")
            if ref == ind.id:
                errs.append(f"{ind.id}: {fname}가 자기 자신을 가리킨다")
        if not ind.sources:
            errs.append(f"{ind.id}: sources가 비었다")
        seen_priority: set[int] = set()
        for entry in ind.sources:
            name = entry.get("name")
            if name not in SOURCE_NAMES:
                errs.append(f"{ind.id}: 알 수 없는 소스 이름 {name!r} {list(SOURCE_NAMES)}")
            via = entry.get("via")
            if via is not None and via not in SOURCE_NAMES:
                errs.append(f"{ind.id}: 알 수 없는 via 소스 {via!r} {list(SOURCE_NAMES)}")
            if not entry.get("series"):
                errs.append(f"{ind.id}: 소스 {name!r}에 series가 없다")
            pr = entry.get("priority")
            if not isinstance(pr, int) or pr < 1:
                errs.append(f"{ind.id}: 소스 {name!r}의 priority는 1 이상 정수여야 한다")
            elif pr in seen_priority:
                errs.append(f"{ind.id}: priority {pr} 중복")
            else:
                seen_priority.add(pr)
            only = entry.get("only")
            if only is not None:
                if not isinstance(only, list):
                    errs.append(f"{ind.id}: 소스 {name!r}의 only는 리스트여야 한다")
                else:
                    bad = [x for x in only if not registry.has_country(str(x))]
                    if bad:
                        errs.append(f"{ind.id}: 소스 {name!r}의 only에 미등록 국가 {bad}")
            for ph in _PLACEHOLDER_RE.findall(f"{entry.get('series', '')} {entry.get('key', '')}"):
                if ph in CODE_KEYS:
                    continue
                if ph in ("iso", "iso3", "name_en", "name_ko", "ccy"):
                    continue
                if ph in ("monthly", "yearly", "fuel"):
                    continue
                errs.append(f"{ind.id}: 소스 {name!r}의 자리표시자 {{{ph}}}를 해석할 수 없다")
        if ind.yoy_from:
            src = registry._by_id.get(str(ind.yoy_from))
            if src is not None and src.native_freq != ind.native_freq:
                errs.append(
                    f"{ind.id}: yoy_from({ind.yoy_from})와 native_freq가 다르다 "
                    f"({src.native_freq} vs {ind.native_freq})"
                )
    return errs
