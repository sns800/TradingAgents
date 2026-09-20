# ============================================================
# [테스트 개요] G20 매크로 수집기 오케스트레이터 (webui/macro/collect.py)
#
# 소스 모듈은 전부 가짜(SOURCE_NAME/CADENCE/collect[/collect_docs])로 바꿔 끼우고 FakeTable 위에서
# 수집기의 조립 로직만 검증한다 — 네트워크·AWS·Bedrock 호출 없음.
#  - 우선순위 병합·fallback_source 플래그 (같은 빈도 충돌, 고빈도 보충, 미참여 소스는 미판정)
#  - fx_usd 월별 D→M 폴백, BIS 월별이 있으면 yahoo 일별 집계가 덮지 않음
#  - fx_usd US 기준값(1.0) 합성 → fx_value_index US = 100
#  - 파생 yoy(직접 관측이 있으면 생략) · fx_value_index(1년 전 대비) · 저빈도 집계(partial_period)
#  - 연간 최후 폴백(World Bank cpi_yoy) 정리 규칙
#  - 케이던스 스킵/--force · poll_of_polls 문서+party_support 관측 · LATEST/SNAPSHOT
#  - INGEST 요약·종료 코드 · --dump/--load 라운드트립 · no-llm 경로 LLM 미생성
#  - 유로 회원국 공유 지표 미생성 · energy_policy 해시 저장/복원
#  - 리뷰 수정 회귀: 케이던스 날짜 경계(고정 21:00 UTC 기동에서 daily가 매일 실행) · 실패 run 1일 재시도
#    · 여론조사 W→M 집계가 poll_of_polls의 derived M에 막히지 않음(같은 버킷만 비교) · 증분 실행의
#    파생 창 축소(since − 시차, 건드린 국가만) · 수집 락(동시 실행 스킵·덤프 제외) · 후처리 예외 격리
#    · LLM 예산 CONFIG 우선 · lcu_bn 지표 rank 없음
# ============================================================
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("boto3", reason="webui deps not installed")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "webui"))

from macro import collect  # noqa: E402
from macro.fakeddb import FakeS3, FakeTable  # noqa: E402
from macro.registry import load_registry  # noqa: E402
from macro.schema import Doc, Observation  # noqa: E402
from macro.store import MacroStore  # noqa: E402

pytestmark = pytest.mark.unit

TODAY = date(2026, 9, 20)
REG = load_registry()


# ------------------------------------------------------------------ 스텁/헬퍼
def _obs(indicator, iso, freq, period, value, source, **kw):
    kw.setdefault("unit", REG.indicator(indicator).unit if REG.has_indicator(indicator) else "%")
    kw.setdefault("series_id", f"{source}:{indicator}")
    kw.setdefault("source_url", f"https://example.test/{source}")
    kw.setdefault("method", "테스트")
    kw.setdefault("vintage", "2026-09-19")
    return Observation(
        indicator=indicator, iso=iso, freq=freq, period=period, value=value, source=source, **kw
    )


def _months(start_year, start_month, n):
    y, m = start_year, start_month
    for _ in range(n):
        yield f"{y:04d}-{m:02d}"
        m += 1
        if m > 12:
            y, m = y + 1, 1


def fake_module(name, cadence="daily", obs=None, docs=None, raise_exc=None, calls=None):
    """가짜 소스 모듈. obs는 리스트 또는 (countries, indicators, ctx) -> list 콜러블."""
    calls = calls if calls is not None else []

    def collect_fn(countries, indicators, ctx):
        calls.append((name, [c.iso for c in countries], [i.id for i in indicators]))
        if raise_exc is not None:
            raise raise_exc
        return list(obs(countries, indicators, ctx)) if callable(obs) else list(obs or [])

    mod = SimpleNamespace(SOURCE_NAME=name, CADENCE=cadence, collect=collect_fn, calls=calls)
    if docs is not None:
        mod.collect_docs = (
            docs if callable(docs) else (lambda countries, ctx: list(docs))
        )
    return mod


def run(monkeypatch, modules, *, countries=("KR", "US"), table=None, s3=None, sources=None,
        force=True, no_llm=True, since=date(2026, 1, 1), today=TODAY, **opt):
    monkeypatch.setattr(collect, "SOURCE_MODULES", {m.SOURCE_NAME: m for m in modules})
    table = table if table is not None else FakeTable()
    s3 = s3 if s3 is not None else FakeS3()
    store = MacroStore(table, s3=s3, bucket="dry-run")
    opts = collect.Options(
        sources=list(sources or [m.SOURCE_NAME for m in modules]),
        countries=list(countries),
        dry_run=True,
        no_llm=no_llm,
        force=force,
        since=since,
        **opt,
    )
    c = collect.Collector(opts, store=store, raw_saver=lambda s, n, d: None, registry=REG, today=today)
    code = c.run()
    return c, store, code


def _series(store, indicator, iso, freq):
    return store.query_series(indicator, iso, freq)


# ------------------------------------------------------------------ 병합 (순수 함수)
class TestMerge:
    def test_same_freq_conflict_picks_higher_priority(self):
        fred = [_obs("dxy", "US", "D", "2026-09-01", 100.0, "fred")]
        yahoo = [
            _obs("dxy", "US", "D", "2026-09-01", 99.0, "yahoo"),
            _obs("dxy", "US", "D", "2026-09-02", 98.0, "yahoo"),
        ]
        out = collect.merge_observations(REG, {"fred": fred, "yahoo": yahoo})
        assert [o.value for o in out["fred"]] == [100.0]
        assert out["yahoo"] == []  # 같은 빈도는 하위 소스 전부 제외 (기간이 더 많아도)
        assert "fallback_source" not in out["fred"][0].flags

    def test_fallback_flag_only_when_higher_source_ran_and_missed(self):
        yahoo = [_obs("dxy", "US", "D", "2026-09-01", 99.0, "yahoo")]
        # fred가 참여했지만 dxy를 못 냄 → 폴백 표시
        out = collect.merge_observations(REG, {"fred": [], "yahoo": yahoo}, ran_sources=["fred", "yahoo"])
        assert out["yahoo"][0].flags == ["fallback_source"]
        # fred가 참여하지 않은 실행(--sources yahoo) → 판정 불가 → 표시 없음
        yahoo2 = [_obs("dxy", "US", "D", "2026-09-01", 99.0, "yahoo")]
        out2 = collect.merge_observations(REG, {"yahoo": yahoo2}, ran_sources=["yahoo"])
        assert out2["yahoo"][0].flags == []

    def test_higher_frequency_supplement_is_kept_without_flag(self):
        bis = [_obs("fx_usd", "KR", "M", "2026-08", 1400.0, "bis")]
        yahoo = [_obs("fx_usd", "KR", "D", "2026-08-28", 1395.0, "yahoo")]
        out = collect.merge_observations(REG, {"bis": bis, "yahoo": yahoo})
        assert len(out["bis"]) == 1 and len(out["yahoo"]) == 1
        assert out["yahoo"][0].flags == []

    def test_lower_frequency_from_lower_source_is_dropped(self):
        oecd = [_obs("cpi_yoy", "KR", "M", "2026-08", 2.0, "oecd")]
        wb = [_obs("cpi_yoy", "KR", "Y", "2025", 2.3, "worldbank")]
        out = collect.merge_observations(REG, {"oecd": oecd, "worldbank": wb})
        assert len(out["oecd"]) == 1 and out["worldbank"] == []

    def test_euro_member_shared_indicator_is_dropped(self):
        bis = [
            _obs("policy_rate", "DE", "D", "2026-09-01", 2.0, "bis"),
            _obs("house_price_index", "DE", "Q", "2026-Q2", 150.0, "bis"),
        ]
        out = collect.merge_observations(REG, {"bis": bis})
        assert [o.indicator for o in out["bis"]] == ["house_price_index"]


# ------------------------------------------------------------------ 실행 흐름
class TestFxFallback:
    def test_monthly_filled_from_daily_when_bis_missing(self, monkeypatch):
        yahoo = fake_module("yahoo", obs=[
            _obs("fx_usd", "KR", "D", "2026-08-03", 1390.0, "yahoo"),
            _obs("fx_usd", "KR", "D", "2026-08-28", 1395.0, "yahoo"),
            _obs("fx_usd", "KR", "D", "2026-09-02", 1380.0, "yahoo"),
        ])
        bis = fake_module("bis", obs=[])  # 참여했지만 실패(빈 결과)
        _, store, code = run(monkeypatch, [yahoo, bis], countries=("KR",))
        assert code == 0
        aug = store.get_observation("fx_usd", "KR", "M", "2026-08")
        sep = store.get_observation("fx_usd", "KR", "M", "2026-09")
        assert aug["value"] == 1395.0 and "fallback_source" in aug["flags"]
        assert aug["source"] == "yahoo"
        assert "partial_period" in sep["flags"] and sep["value"] == 1380.0
        # 일별도 폴백 표시 (BIS가 참여했는데 KR 환율을 못 냄)
        assert "fallback_source" in store.get_observation("fx_usd", "KR", "D", "2026-08-03")["flags"]

    def test_bis_monthly_is_not_overwritten_by_daily_aggregate(self, monkeypatch):
        bis = fake_module("bis", obs=[_obs("fx_usd", "KR", "M", "2026-08", 1400.0, "bis")])
        yahoo = fake_module("yahoo", obs=[
            _obs("fx_usd", "KR", "D", "2026-08-28", 1395.0, "yahoo"),
            _obs("fx_usd", "KR", "D", "2026-09-02", 1380.0, "yahoo"),
        ])
        _, store, _ = run(monkeypatch, [bis, yahoo], countries=("KR",))
        aug = store.get_observation("fx_usd", "KR", "M", "2026-08")
        assert aug["value"] == 1400.0 and aug["source"] == "bis"
        assert "fallback_source" not in aug["flags"]
        # BIS가 월별을 소유하므로 9월 월별을 yahoo 집계로 만들지 않는다
        assert store.get_observation("fx_usd", "KR", "M", "2026-09") is None
        # 분기는 월별(BIS)에서 집계
        q3 = store.get_observation("fx_usd", "KR", "Q", "2026-Q3")
        assert q3["value"] == 1400.0 and q3["source"] == "bis"


class TestUsFxBase:
    """US fx_usd는 기준통화라 어떤 소스도 내지 않아 수집기가 1.0을 합성한다 (CONTRACT 12장)."""

    def test_us_monthly_synthesized_over_other_countries_range(self, monkeypatch):
        fx = [_obs("fx_usd", "KR", "M", p, 1300.0 + i, "bis") for i, p in enumerate(_months(2025, 1, 20))]
        _, store, _ = run(monkeypatch, [fake_module("bis", obs=fx)], countries=("KR", "US"))
        us = _series(store, "fx_usd", "US", "M")
        assert [r["period"] for r in us] == list(_months(2025, 1, 20))
        assert {r["value"] for r in us} == {1.0}
        first = us[0]
        assert first["source"] == "derived" and first["series_id"] == "USD_BASE"
        assert first["unit"] == "lcu_per_usd" and first["flags"] == ["derived"]
        assert first["source_url"] == "https://www.federalreserve.gov/releases/h10/"
        assert "기준통화(USD) = 1.0" in first["method"]
        assert _series(store, "fx_usd", "US", "D") == []  # D 빈도는 만들지 않는다
        # 파생: 통화가치 지수 US = 100 (순위에도 US가 100으로 들어간다 — 의도된 동작)
        assert store.get_observation("fx_value_index", "US", "M", "2026-01")["value"] == pytest.approx(100.0)
        latest = {r["sk"]: r for r in store.get_latest("fx_value_index")}
        assert latest["US"]["value"] == pytest.approx(100.0) and latest["US"]["n"] == 2

    def test_gap_months_are_filled(self, monkeypatch):
        fx = [
            _obs("fx_usd", "KR", "M", "2026-06", 1390.0, "bis"),
            _obs("fx_usd", "JP", "M", "2026-08", 150.0, "bis"),
        ]
        _, store, _ = run(monkeypatch, [fake_module("bis", obs=fx)], countries=("KR", "JP", "US"))
        assert [r["period"] for r in _series(store, "fx_usd", "US", "M")] == [
            "2026-06", "2026-07", "2026-08",
        ]

    def test_filled_from_daily_fallback_range_too(self, monkeypatch):
        yahoo = fake_module("yahoo", obs=[
            _obs("fx_usd", "KR", "D", "2026-08-28", 1395.0, "yahoo"),
            _obs("fx_usd", "KR", "D", "2026-09-02", 1380.0, "yahoo"),
        ])
        _, store, _ = run(monkeypatch, [yahoo], countries=("KR", "US"))
        assert [r["period"] for r in _series(store, "fx_usd", "US", "M")] == ["2026-08", "2026-09"]

    def test_not_synthesized_without_us_target_or_source_value(self, monkeypatch):
        fx = [_obs("fx_usd", "KR", "M", "2026-08", 1400.0, "bis")]
        _, store, _ = run(monkeypatch, [fake_module("bis", obs=fx)], countries=("KR",))
        assert _series(store, "fx_usd", "US", "M") == []  # US가 대상 국가가 아님
        # 소스가 US 환율을 내면 합성하지 않고 그 값을 쓴다
        fx2 = fx + [_obs("fx_usd", "US", "M", "2026-08", 1.0, "bis")]
        _, store2, _ = run(monkeypatch, [fake_module("bis", obs=fx2)], countries=("KR", "US"))
        assert [(r["period"], r["source"]) for r in _series(store2, "fx_usd", "US", "M")] == [
            ("2026-08", "bis")
        ]

    def test_pure_function_ignores_non_monthly_and_us_rows(self):
        rows = [
            _obs("fx_usd", "KR", "D", "2026-08-28", 1395.0, "yahoo"),
            _obs("fx_usd", "US", "M", "2026-08", 1.0, "bis"),
        ]
        assert collect.synth_us_fx_monthly(rows) == []


class TestDerived:
    def test_yoy_derived_and_skipped_when_direct_exists(self, monkeypatch):
        kr = [_obs("cpi_index", "KR", "M", p, 100.0 + i, "oecd")
              for i, p in enumerate(_months(2024, 1, 32))]  # 2024-01..2026-08
        us_index = [_obs("cpi_index", "US", "M", p, 200.0 + i, "oecd")
                    for i, p in enumerate(_months(2024, 1, 32))]
        us_direct = [_obs("cpi_yoy", "US", "M", "2026-08", 2.5, "oecd")]
        oecd = fake_module("oecd", "weekly", obs=kr + us_index + us_direct)
        _, store, _ = run(monkeypatch, [oecd])
        yoy = store.get_observation("cpi_yoy", "KR", "M", "2025-01")
        assert yoy is not None and yoy["source"] == "derived" and "derived" in yoy["flags"]
        assert yoy["value"] == pytest.approx((112.0 / 100.0 - 1) * 100)
        # 직접 관측(OECD GY)이 있는 US는 파생하지 않는다
        us = _series(store, "cpi_yoy", "US", "M")
        assert [(r["period"], r["source"]) for r in us] == [("2026-08", "oecd")]
        # 파생 M에서 분기·연간 집계 (agg=last, 진행 중 기간 표시)
        q = store.get_observation("cpi_yoy", "KR", "Q", "2026-Q3")
        assert q["value"] == store.get_observation("cpi_yoy", "KR", "M", "2026-08")["value"]
        assert "partial_period" in q["flags"]

    def test_fx_value_index_is_ratio_to_twelve_months_ago(self, monkeypatch):
        fx = [_obs("fx_usd", "KR", "M", p, 1300.0 + i, "bis") for i, p in enumerate(_months(2025, 1, 20))]
        bis = fake_module("bis", obs=fx)
        _, store, _ = run(monkeypatch, [bis], countries=("KR",))
        jan = store.get_observation("fx_value_index", "KR", "M", "2026-01")
        assert jan["value"] == pytest.approx(1300.0 / 1312.0 * 100)
        assert jan["unit"] == "index" and jan["source"] == "derived"
        assert "1년 전 대비" in jan["method"]
        assert store.get_observation("fx_value_index", "KR", "M", "2025-06") is None  # 12개월 전 없음
        assert store.get_observation("fx_value_index", "KR", "D", "2026-01-01") is None  # D는 만들지 않음
        q1 = store.get_observation("fx_value_index", "KR", "Q", "2026-Q1")
        assert q1["value"] == store.get_observation("fx_value_index", "KR", "M", "2026-03")["value"]
        y = store.get_observation("fx_value_index", "KR", "Y", "2026")
        assert "partial_period" in y["flags"]

    def test_low_freq_aggregation_from_daily(self, monkeypatch):
        daily = [
            _obs("policy_rate", "KR", "D", "2026-01-15", 3.0, "bis"),
            _obs("policy_rate", "KR", "D", "2026-03-31", 2.75, "bis"),
            _obs("policy_rate", "KR", "D", "2026-07-10", 2.5, "bis"),
            _obs("policy_rate", "KR", "D", "2026-09-18", 2.25, "bis"),
        ]
        bis = fake_module("bis", obs=daily)
        _, store, _ = run(monkeypatch, [bis], countries=("KR",))
        assert store.get_observation("policy_rate", "KR", "M", "2026-03")["value"] == 2.75
        q1 = store.get_observation("policy_rate", "KR", "Q", "2026-Q1")
        assert q1["value"] == 2.75 and "partial_period" not in q1["flags"]
        q3 = store.get_observation("policy_rate", "KR", "Q", "2026-Q3")
        assert q3["value"] == 2.25 and "partial_period" in q3["flags"]
        assert store.get_observation("policy_rate", "KR", "Y", "2026")["value"] == 2.25

    def test_annual_fallback_pruned_when_monthly_path_exists(self, monkeypatch):
        kr_index = [_obs("cpi_index", "KR", "M", p, 100.0 + i, "oecd") for i, p in enumerate(_months(2024, 1, 32))]
        oecd = fake_module("oecd", obs=kr_index)
        wb = fake_module("worldbank", "monthly", obs=[
            _obs("cpi_yoy", "KR", "Y", "2025", 2.3, "worldbank"),
            _obs("cpi_yoy", "US", "Y", "2025", 2.9, "worldbank"),
        ])
        _, store, _ = run(monkeypatch, [oecd, wb])
        # KR: 월별 파생 경로가 있어 연간 폴백 제외
        assert store.get_observation("cpi_yoy", "KR", "Y", "2025")["source"] == "derived"
        # US: 월별 경로 없음 → 연간 폴백 채택 + 표시
        us = store.get_observation("cpi_yoy", "US", "Y", "2025")
        assert us["source"] == "worldbank" and "fallback_source" in us["flags"]

    def test_incremental_run_narrows_derivation_window_and_countries(self, monkeypatch):
        """증분 실행(since 有): yoy·fx_value_index 조회를 since − 시차부터, 이번 실행이 건드린 국가만."""
        table = FakeTable()
        seed = MacroStore(table)
        seed.put_observations(
            [_obs("cpi_index", iso, "M", p, base + i, "oecd")
             for iso, base in (("KR", 100.0), ("US", 200.0))
             for i, p in enumerate(_months(2020, 1, 79))]  # 2020-01..2026-07
            + [_obs("fx_usd", iso, "M", p, 1300.0 + i, "bis")
               for iso in ("KR", "JP") for i, p in enumerate(_months(2020, 1, 79))]
        )
        calls = []
        orig = MacroStore.query_series

        def spy(self, indicator, iso, freq, from_period=None, to_period=None, limit=None):
            calls.append((indicator, iso, freq, from_period, limit))
            return orig(self, indicator, iso, freq, from_period=from_period, to_period=to_period, limit=limit)

        monkeypatch.setattr(MacroStore, "query_series", spy)
        oecd = fake_module("oecd", "weekly", obs=[_obs("cpi_index", "KR", "M", "2026-08", 179.0, "oecd")])
        bis = fake_module("bis", obs=[_obs("fx_usd", "KR", "M", "2026-08", 1379.0, "bis")])
        _, store, _ = run(monkeypatch, [oecd, bis], countries=("KR", "US", "JP"), table=table,
                          since=date(2026, 7, 1))
        table.calls.clear()
        # yoy 원천 조회: KR만, 창은 2026-07 − 12개월 = 2025-07부터. (limit 없는 다른 cpi_index 조회는
        # M→Q/Y 집계의 버킷 시작(2026-07·2026-01)이며, 전 범위(from_period None) 조회는 없다)
        cpi_calls = [c for c in calls if c[0] == "cpi_index" and c[4] is None]
        assert cpi_calls and all(c[1] == "KR" for c in cpi_calls)
        assert any(c[3] == "2025-07" for c in cpi_calls) and all(c[3] is not None for c in cpi_calls)
        # 통화가치 지수 원천 조회: KR + 합성된 US(=1.0)만, 2025-07부터 (JP는 이번 실행이 건드리지 않음)
        fx_calls = [c for c in calls if c[0] == "fx_usd" and c[2] == "M" and c[4] is None]
        assert fx_calls and {c[1] for c in fx_calls} == {"KR", "US"}
        assert any(c[3] == "2025-07" for c in fx_calls) and all(c[3] is not None for c in fx_calls)
        # 결과는 정확: 2026-08 yoy = 179/167 − 1, 2026-07 yoy도 창 안이라 계산됨
        assert store.get_observation("cpi_yoy", "KR", "M", "2026-08")["value"] == pytest.approx((179.0 / 167.0 - 1) * 100)
        assert store.get_observation("cpi_yoy", "KR", "M", "2026-07")["value"] == pytest.approx((178.0 / 166.0 - 1) * 100)
        assert store.get_observation("cpi_yoy", "US", "M", "2026-07") is None  # 건드리지 않은 국가
        assert store.get_observation("fx_value_index", "KR", "M", "2026-08")["value"] == pytest.approx(1367.0 / 1379.0 * 100)
        assert store.get_observation("fx_value_index", "JP", "M", "2026-07") is None

        # 전체 수집(since None) — 첫 실행이라 INGEST가 없어 전 범위·전 국가로 계산한다
        calls.clear()
        oecd2 = fake_module("oecd", "weekly", obs=[_obs("cpi_index", "KR", "M", "2026-08", 179.0, "oecd")])
        _, store2, _ = run(monkeypatch, [oecd2], countries=("KR", "US"), table=FakeTable(), since=None)
        full = [c for c in calls if c[0] == "cpi_index" and c[4] is None and c[3] is None]
        assert {c[1] for c in full} == {"KR", "US"}  # 두 국가 모두 전 범위(from_period None) 조회
        assert store2.get_observation("cpi_yoy", "KR", "M", "2026-08") is None  # 12개월 전 없음(빈 테이블)

    def test_skip_derived_option(self, monkeypatch):
        fx = [_obs("fx_usd", "KR", "M", p, 1300.0 + i, "bis") for i, p in enumerate(_months(2025, 1, 20))]
        _, store, _ = run(monkeypatch, [fake_module("bis", obs=fx)], countries=("KR",), skip_derived=True)
        assert _series(store, "fx_value_index", "KR", "M") == []
        assert _series(store, "fx_usd", "KR", "Q") == []


class TestCadence:
    def test_skip_until_next_due_and_force(self, monkeypatch):
        bis = fake_module("bis", obs=[_obs("policy_rate", "KR", "D", "2026-09-18", 2.25, "bis")])
        table = FakeTable()
        _, store, _ = run(monkeypatch, [bis], countries=("KR",), table=table)
        latest = store.get_ingest_latest("bis")
        assert latest["status"] == "ok" and latest["next_due"] > latest["finished_at"]
        assert latest["cadence_days"] == 1
        n_calls = len(bis.calls)
        c2, _, code = run(monkeypatch, [bis], countries=("KR",), table=table, force=False)
        assert code == 0 and c2.runs["bis"].status == "skipped" and len(bis.calls) == n_calls
        # 건너뛴 소스는 INGEST를 다시 쓰지 않는다
        assert store.get_ingest_latest("bis")["finished_at"] == latest["finished_at"]
        run(monkeypatch, [bis], countries=("KR",), table=table, force=True)
        assert len(bis.calls) == n_calls + 1

    def test_daily_source_runs_every_day_with_fixed_start_time(self, monkeypatch):
        """리뷰 재현: day1 21:03 종료 → next_due day2 00:00 → day2 21:00 기동에서 실행된다."""
        bis = fake_module("bis", obs=[_obs("policy_rate", "KR", "D", "2026-09-18", 2.25, "bis")])
        table = FakeTable()
        store = MacroStore(table)
        now = datetime.now(timezone.utc)
        yesterday = (now - timedelta(days=1)).replace(hour=21, minute=3, second=0, microsecond=0)
        latest = store.log_ingest("bis", {"status": "ok", "finished_at": yesterday.isoformat(timespec="seconds"),
                                          "cadence_days": 1})
        expected_due = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        assert latest["next_due"] == expected_due.isoformat(timespec="seconds")
        c, _, _ = run(monkeypatch, [bis], countries=("KR",), table=table, force=False)
        assert c.runs["bis"].status == "ok" and len(bis.calls) == 1
        # 오늘 이미 끝난 소스는 다음 날 00:00 UTC까지 건너뛴다
        c2, _, _ = run(monkeypatch, [bis], countries=("KR",), table=table, force=False)
        assert c2.runs["bis"].status == "skipped" and len(bis.calls) == 1
        due = store.get_ingest_latest("bis")["next_due"]
        assert due.endswith("T00:00:00+00:00") and due > now.isoformat()

    def test_failed_or_partial_run_retries_next_day(self, monkeypatch):
        def partial(countries, indicators, ctx):
            ctx.record_error("oecd", "US", "HTTP 500")
            return [_obs("cpi_index", "KR", "M", "2026-08", 120.0, "oecd")]

        oecd = fake_module("oecd", "weekly", obs=partial)
        table = FakeTable()
        _, store, _ = run(monkeypatch, [oecd], table=table)
        latest = store.get_ingest_latest("oecd")
        finished = datetime.fromisoformat(latest["finished_at"]).astimezone(timezone.utc)
        tomorrow = datetime(finished.year, finished.month, finished.day, tzinfo=timezone.utc) + timedelta(days=1)
        assert latest["status"] == "partial" and latest["cadence_days"] == 7
        assert latest["next_due"] == tomorrow.isoformat(timespec="seconds")
        assert "partial" in latest["retry_reason"]

        boom = fake_module("imf", "weekly", raise_exc=RuntimeError("down"))
        _, store2, _ = run(monkeypatch, [boom], table=FakeTable())
        failed = store2.get_ingest_latest("imf")
        assert failed["status"] == "failed" and failed["cadence_days"] == 7
        assert failed["next_due"].endswith("T00:00:00+00:00") and "failed" in failed["retry_reason"]

    def test_since_defaults_to_last_run_minus_lookback(self, monkeypatch):
        bis = fake_module("bis", obs=[_obs("policy_rate", "KR", "D", "2026-09-18", 2.25, "bis")])
        table = FakeTable()
        run(monkeypatch, [bis], countries=("KR",), table=table)
        c2, _, _ = run(monkeypatch, [bis], countries=("KR",), table=table, since=None)
        expected = date.today() - __import__("datetime").timedelta(days=collect.INCREMENTAL_LOOKBACK_DAYS)
        assert abs((c2.ctx.since - expected).days) <= 1


class TestPolls:
    def test_poll_of_polls_doc_and_monthly_observations(self, monkeypatch):
        table = FakeTable()
        store = MacroStore(table)
        store.put_docs([
            Doc(type="election", iso="KR", date="2026-09-01", id="e" * 12, title_ko="선거", summary_ko="",
                source_url="https://en.wikipedia.org/wiki/x", source_name="yaml",
                payload={"ruling_party": "더불어민주당"}),
            Doc(type="poll", iso="KR", date="2026-09-15", id="a" * 12, title_ko="p1", summary_ko="",
                source_url="https://en.wikipedia.org/wiki/x", source_name="갤럽",
                payload={"pollster": "갤럽", "fieldwork_end": "2026-09-15", "sample_size": 1000,
                         "results": {"더불어민주당": 45.0, "국민의힘": 35.0}, "gov_approval": 55.0}),
            Doc(type="poll", iso="KR", date="2026-09-10", id="b" * 12, title_ko="p2", summary_ko="",
                source_url="https://en.wikipedia.org/wiki/x", source_name="리얼미터",
                payload={"pollster": "리얼미터", "fieldwork_end": "2026-09-10", "sample_size": 1000,
                         "results": {"더불어민주당": 41.0, "국민의힘": 39.0}, "gov_approval": 51.0}),
        ])
        dummy = fake_module("wiki_polls", "weekly", obs=[], docs=[])
        _, store, _ = run(monkeypatch, [dummy], countries=("KR",), table=table)
        pop = store.latest_doc("poll_of_polls", "KR")
        assert pop is not None and pop["ai_generated"] is False and pop["review_status"] == "approved"
        payload = pop["payload"]
        assert payload["ruling_party"] == "더불어민주당" and payload["n_polls"] == 2
        assert 41.0 < payload["ruling_pct"] < 45.0 and payload["leader_party"] == "더불어민주당"
        ps = store.get_observation("party_support", "KR", "M", "2026-09")
        assert ps["value"] == payload["ruling_pct"] and ps["payload"]["results"]["국민의힘"] == payload["results"]["국민의힘"]
        assert store.get_observation("gov_approval", "KR", "M", "2026-09")["value"] == payload["gov_approval"]
        # LATEST/SNAPSHOT에도 반영
        latest = store.get_latest("party_support", ["KR"])
        assert latest and latest[0]["period"] == "2026-09" and latest[0]["freq"] == "M"
        assert store.get_snapshot("KR")["docs"]["poll_of_polls"]["payload"]["n_polls"] == 2

    def test_weekly_polls_current_month_left_to_poll_of_polls(self, monkeypatch):
        weekly = [
            _obs("party_support", "KR", "W", "2026-08-05", 40.0, "wiki_polls", payload={"results": {"A": 40.0}}),
            _obs("party_support", "KR", "W", "2026-08-19", 44.0, "wiki_polls", payload={"results": {"A": 44.0}}),
            _obs("party_support", "KR", "W", "2026-09-16", 46.0, "wiki_polls", payload={"results": {"A": 46.0}}),
        ]
        polls = fake_module("wiki_polls", "weekly", obs=weekly, docs=[])
        _, store, _ = run(monkeypatch, [polls], countries=("KR",))
        assert store.get_observation("party_support", "KR", "M", "2026-08")["value"] == 42.0  # 평균
        assert store.get_observation("party_support", "KR", "M", "2026-09") is None  # 진행 중 → poll_of_polls 몫


    def test_monthly_aggregation_not_blocked_by_poll_of_polls_month(self, monkeypatch):
        """리뷰 재현: 7월 M 생성 → 9월 poll_of_polls M(derived) → 8월 W 도착 시 8월 M도 생성돼야 한다."""
        table = FakeTable()
        july = [
            _obs("party_support", "KR", "W", "2026-07-08", 38.0, "wiki_polls", payload={"results": {"A": 38.0}}),
            _obs("party_support", "KR", "W", "2026-07-22", 40.0, "wiki_polls", payload={"results": {"A": 40.0}}),
        ]
        run(monkeypatch, [fake_module("wiki_polls", "weekly", obs=july, docs=[])], countries=("KR",),
            table=table, today=date(2026, 8, 10))
        store = MacroStore(table)
        assert store.get_observation("party_support", "KR", "M", "2026-07")["value"] == 39.0

        # 9월 초: 여론조사 문서만 있고 W 관측은 없음 → poll_of_polls가 2026-09 M(derived)을 만든다
        store.put_docs([
            Doc(type="election", iso="KR", date="2026-09-01", id="e" * 12, title_ko="선거", summary_ko="",
                source_url="https://en.wikipedia.org/wiki/x", source_name="yaml", payload={"ruling_party": "A"}),
            Doc(type="poll", iso="KR", date="2026-09-03", id="a" * 12, title_ko="p1", summary_ko="",
                source_url="https://en.wikipedia.org/wiki/x", source_name="갤럽",
                payload={"pollster": "갤럽", "fieldwork_end": "2026-09-03", "sample_size": 1000,
                         "results": {"A": 46.0, "B": 30.0}, "gov_approval": 50.0}),
        ])
        run(monkeypatch, [fake_module("wiki_polls", "weekly", obs=[], docs=[])], countries=("KR",),
            table=table, today=date(2026, 9, 5))
        sep = store.get_observation("party_support", "KR", "M", "2026-09")
        assert sep is not None and sep["source"] == "derived"
        assert [r["period"] for r in store.query_series("party_support", "KR", "M")] == ["2026-07", "2026-09"]

        # 9월 중순: 8월 W 관측이 (늦게) 도착 → 8월 M이 만들어져야 한다. 9월 derived는 그대로.
        august = [
            _obs("party_support", "KR", "W", "2026-08-05", 40.0, "wiki_polls", payload={"results": {"A": 40.0}}),
            _obs("party_support", "KR", "W", "2026-08-19", 44.0, "wiki_polls", payload={"results": {"A": 44.0}}),
            _obs("party_support", "KR", "W", "2026-09-16", 47.0, "wiki_polls", payload={"results": {"A": 47.0}}),
        ]
        run(monkeypatch, [fake_module("wiki_polls", "weekly", obs=august, docs=[])], countries=("KR",),
            table=table, today=date(2026, 9, 20))
        months = store.query_series("party_support", "KR", "M")
        assert [r["period"] for r in months] == ["2026-07", "2026-08", "2026-09"]
        by = {r["period"]: r for r in months}
        assert by["2026-08"]["value"] == 42.0 and by["2026-08"]["source"] == "wiki_polls"
        assert by["2026-09"]["source"] == "derived" and by["2026-09"]["value"] == sep["value"]


class TestLock:
    def test_second_collector_skipped_while_lock_held_and_released_after_run(self, monkeypatch):
        table = FakeTable()
        store = MacroStore(table)
        assert store.acquire_lock("other-run") is True
        bis = fake_module("bis", obs=[_obs("policy_rate", "KR", "D", "2026-09-18", 2.25, "bis")])
        c, _, code = run(monkeypatch, [bis], countries=("KR",), table=table)
        assert code == 0 and c.lock_skipped is True and bis.calls == [] and c.runs == {}
        assert store.get_ingest_latest("bis") is None
        assert store.get_lock()["holder"] == "other-run"  # 남의 락은 건드리지 않는다

        store.release_lock("other-run")
        c2, _, code2 = run(monkeypatch, [bis], countries=("KR",), table=table)
        assert code2 == 0 and c2.lock_skipped is False and len(bis.calls) == 1
        assert store.get_lock() is None  # finally에서 해제
        assert store.get_ingest_latest("bis")["status"] == "ok"

    def test_lock_released_even_when_run_fails_and_excluded_from_dump(self, monkeypatch, tmp_path):
        table = FakeTable()
        boom = fake_module("bis", raise_exc=RuntimeError("boom"))
        dump = tmp_path / "d.json"
        c, store, code = run(monkeypatch, [boom], countries=("KR",), table=table, dump=str(dump))
        assert code == 1 and store.get_lock() is None
        raw = json.loads(dump.read_text(encoding="utf-8"))
        assert not any(it["pk"] == "CONFIG" and it["sk"] == "LOCK#collect" for it in raw["table"])

        # 예외로 중단돼도 finally가 락을 지운다
        monkeypatch.setattr(collect.Collector, "_run_locked", lambda self, t0: (_ for _ in ()).throw(RuntimeError("x")))
        with pytest.raises(RuntimeError):
            run(monkeypatch, [boom], countries=("KR",), table=table)
        assert store.get_lock() is None


class TestRebuildAndIngest:
    def test_post_processing_failure_is_isolated(self, monkeypatch):
        """파생 단계가 예외를 던져도 poll_of_polls·LATEST/SNAPSHOT·INGEST는 기록된다."""
        def explode(self, ind):
            raise RuntimeError("derive exploded")

        monkeypatch.setattr(collect.Collector, "_derive_yoy", explode)
        kr = [_obs("cpi_index", "KR", "M", p, 100.0 + i, "oecd") for i, p in enumerate(_months(2024, 1, 32))]
        oecd = fake_module("oecd", "weekly", obs=kr)
        c, store, code = run(monkeypatch, [oecd], countries=("KR",))
        assert code == 0
        assert any(e.startswith("derive:cpi_yoy") for e in c.post_errors)
        assert store.get_latest("cpi_index", ["KR"])[0]["period"] == "2026-08"
        assert "cpi_index" in store.get_snapshot("KR")["latest"]
        ing = store.get_ingest_latest("oecd")
        assert ing["status"] == "ok" and ing["n_obs"] == 32
        assert any("derive exploded" in e for e in ing["post_errors"])

        # 단계 전체(_rebuild)가 죽어도 INGEST는 남는다
        monkeypatch.setattr(collect.Collector, "_rebuild", lambda self: (_ for _ in ()).throw(RuntimeError("rebuild")))
        c2, store2, code2 = run(monkeypatch, [oecd], countries=("KR",))
        assert code2 == 0 and any(e.startswith("rebuild:") for e in c2.post_errors)
        assert store2.get_ingest_latest("oecd")["status"] == "ok"
        assert store2.get_latest("cpi_index") == []

    def test_lcu_indicator_latest_has_no_rank(self, monkeypatch):
        imf = fake_module("imf", "weekly", obs=[
            _obs("m2_level", "KR", "M", "2026-08", 4000.0, "imf", unit="lcu_bn"),
            _obs("m2_level", "JP", "M", "2026-08", 1200.0, "imf", unit="lcu_bn"),
            _obs("m2_yoy", "KR", "M", "2026-08", 6.0, "imf"),
            _obs("m2_yoy", "JP", "M", "2026-08", 1.5, "imf"),
        ])
        _, store, _ = run(monkeypatch, [imf], countries=("KR", "JP"))
        level = {r["sk"]: r for r in store.get_latest("m2_level")}
        assert set(level) == {"KR", "JP"}
        assert all(r["rank"] is None and r["n"] == 0 for r in level.values())
        yoy = {r["sk"]: r for r in store.get_latest("m2_yoy")}
        assert yoy["KR"]["rank"] == 1 and yoy["JP"]["rank"] == 2 and yoy["KR"]["n"] == 2

    def test_latest_snapshot_and_ingest_summary(self, monkeypatch):
        def collect_fn(countries, indicators, ctx):
            ctx.record_error("bis", "US", "HTTP 500")  # 국가별 실패 격리 (CONTRACT 7장)
            return [
                _obs("policy_rate", "KR", "D", "2026-09-18", 2.25, "bis"),
                _obs("policy_rate", "JP", "D", "2026-09-18", 0.75, "bis"),
            ]

        bis = fake_module("bis", obs=collect_fn)
        c, store, code = run(monkeypatch, [bis], countries=("KR", "US", "JP"))
        assert code == 0
        latest = {r["sk"]: r for r in store.get_latest("policy_rate")}
        assert set(latest) == {"KR", "JP"}  # 값이 없는 US는 LATEST를 만들지 않는다
        assert latest["KR"]["rank"] == 1 and latest["JP"]["rank"] == 2 and latest["KR"]["n"] == 2
        snap = store.get_snapshot("KR")
        assert snap["latest"]["policy_rate"]["value"] == 2.25
        ing = store.get_ingest_latest("bis")
        assert ing["status"] == "partial" and ing["countries_failed"] == ["US"]
        assert set(ing["countries_ok"]) == {"KR", "JP"}
        assert ing["n_obs"] == 2 and ing["n_new"] == 2 and ing["cadence_days"] == 1
        assert ing["errors"] == ["bis:US: HTTP 500"]
        assert c.n_latest >= 2 and c.n_snapshot == 20

    def test_exit_code_when_all_sources_fail(self, monkeypatch):
        boom = fake_module("bis", raise_exc=RuntimeError("boom"))
        c, store, code = run(monkeypatch, [boom], countries=("KR",))
        assert code == 1 and c.runs["bis"].status == "failed"
        assert store.get_ingest_latest("bis")["status"] == "failed"
        ok = fake_module("yahoo", obs=[_obs("dxy", "US", "D", "2026-09-18", 97.0, "yahoo")])
        _, _, code2 = run(monkeypatch, [boom, ok], countries=("KR", "US"))
        assert code2 == 0

    def test_countries_union_across_indicators(self, monkeypatch):
        bis = fake_module("bis", obs=[])
        yahoo = fake_module("yahoo", obs=[])
        run(monkeypatch, [bis, yahoo], countries=("KR", "DE", "EU"))
        # BIS는 독일 집값(house_price_index) 때문에 DE를 받고, yahoo(환율만)는 DE를 받지 않는다
        assert "DE" in bis.calls[0][1] and "DE" not in yahoo.calls[0][1]
        assert "house_price_index" in bis.calls[0][2] and yahoo.calls[0][2] == ["fx_usd", "dxy"]


class TestEuroAndLlm:
    def test_euro_member_shared_indicators_not_generated(self, monkeypatch):
        bis = fake_module("bis", obs=[
            _obs("policy_rate", "DE", "D", "2026-09-18", 2.0, "bis"),
            _obs("fx_usd", "DE", "M", "2026-08", 0.9, "bis"),
            _obs("policy_rate", "EU", "D", "2026-09-18", 2.0, "bis"),
            _obs("house_price_index", "DE", "Q", "2026-Q2", 150.0, "bis"),
        ])
        _, store, _ = run(monkeypatch, [bis], countries=("DE", "EU"))
        assert _series(store, "policy_rate", "DE", "D") == []
        assert _series(store, "fx_usd", "DE", "M") == []
        assert _series(store, "fx_value_index", "DE", "M") == []
        assert {r["sk"] for r in store.get_latest("policy_rate")} == {"EU"}
        assert store.get_observation("house_price_index", "DE", "Q", "2026-Q2") is not None
        assert "house_price_index" in store.get_snapshot("DE")["latest"]

    def test_no_llm_path_never_creates_bedrock_client(self, monkeypatch):
        created = []

        class FakeLLM:
            model_id = "fake"

            def __init__(self, **kw):
                created.append(kw)
                self.kw = kw

        monkeypatch.setattr(collect, "BedrockJson", FakeLLM)
        cb = fake_module("cb_statements", obs=[], docs=[])
        c, store, _ = run(monkeypatch, [cb], countries=("KR",), no_llm=True)
        assert created == [] and c.ctx.llm is None and c.ctx.no_llm is True
        assert "seen_urls" in c.ctx.extra and "seen_hashes" in c.ctx.extra
        # --llm: 예산 콜백이 CONFIG#MACRO를 참조한다
        monkeypatch.setenv("MACRO_LLM_DAILY_TOKEN_BUDGET", "5000")
        c2, store2, _ = run(monkeypatch, [cb], countries=("KR",), no_llm=False)
        assert len(created) == 1 and isinstance(c2.ctx.llm, FakeLLM)
        assert created[0]["budget_check"](4000) is True and created[0]["budget_check"](6000) is False
        created[0]["on_tokens"](3000)
        assert created[0]["budget_check"](3000) is False
        assert store2.get_config()["llm_daily_token_budget"] == 5000

    def test_llm_budget_prefers_config_over_env(self, monkeypatch):
        class FakeLLM:
            model_id = "fake"

            def __init__(self, **kw):
                self.kw = kw

        monkeypatch.setattr(collect, "BedrockJson", FakeLLM)
        cb = fake_module("cb_statements", obs=[], docs=[])
        table = FakeTable()
        MacroStore(table).set_config(llm_daily_token_budget=7000)
        monkeypatch.setenv("MACRO_LLM_DAILY_TOKEN_BUDGET", "5000")
        c, store, _ = run(monkeypatch, [cb], countries=("KR",), table=table, no_llm=False)
        assert store.get_config()["llm_daily_token_budget"] == 7000  # env가 CONFIG를 덮지 않는다
        assert c.ctx.llm.kw["budget_check"](6500) is True and c.ctx.llm.kw["budget_check"](7500) is False
        # CONFIG가 없으면 env가 초기값
        c2, store2, _ = run(monkeypatch, [cb], countries=("KR",), table=FakeTable(), no_llm=False)
        assert store2.get_config()["llm_daily_token_budget"] == 5000

    def test_llm_docs_saved_with_text_and_energy_hashes_roundtrip(self, monkeypatch):
        sha = "ab" * 20
        doc_id = collect._sha12(f"KR:{sha}")

        def collect_docs(countries, ctx):
            if sha in ctx.extra["seen_hashes"]:
                return []
            return [Doc(type="energy_policy", iso="KR", date="2026-09-20", id=doc_id,
                        title_ko="한국 에너지 목표", summary_ko="요약", source_url="https://iea.test",
                        source_name="IEA", payload={"targets": ["t"], "recent_changes": [], "sources": []},
                        ai_generated=True, quotes=["원문 인용"], confidence=0.8)]

        energy = fake_module("energy_policy", "weekly", obs=[], docs=collect_docs)
        table, s3 = FakeTable(), FakeS3()
        c, store, _ = run(monkeypatch, [energy], countries=("KR",), table=table, s3=s3)
        assert c.runs["energy_policy"].n_docs == 1
        saved = store.latest_doc("energy_policy", "KR")
        assert saved["review_status"] == "pending" and saved["s3_key"] == f"macro/docs/energy_policy/KR/2026-09-20_{doc_id}.md"
        assert saved["s3_key"] in s3.objects
        assert store.get_ingest_latest("energy_policy")["hashes"] == {"KR": sha}
        # 다음 실행: 해시가 복원되어 문서를 다시 만들지 않고 해시도 유지
        c2, store2, _ = run(monkeypatch, [energy], countries=("KR",), table=table, s3=s3)
        assert c2.runs["energy_policy"].n_docs == 0
        assert store2.get_ingest_latest("energy_policy")["hashes"] == {"KR": sha}


    def test_energy_hashes_reported_by_module_are_used(self, monkeypatch):
        """모듈이 ctx.extra["energy_hashes"]를 남기면 문서 id 역산(_ProbeSet) 없이 그대로 쓴다."""
        sha = "cd" * 20

        def collect_docs(countries, ctx):
            if sha in ctx.extra["seen_hashes"]:
                return []
            ctx.extra.setdefault("energy_hashes", {})["KR"] = sha
            return [Doc(type="energy_policy", iso="KR", date="2026-09-20", id="f" * 12,
                        title_ko="한국 에너지 목표", summary_ko="요약", source_url="https://iea.test",
                        source_name="IEA",
                        payload={"targets": ["t"], "recent_changes": [], "sources": [],
                                 "content_sha1": sha},
                        ai_generated=True, quotes=["원문 인용"], confidence=0.8)]

        energy = fake_module("energy_policy", "weekly", obs=[], docs=collect_docs)
        table, s3 = FakeTable(), FakeS3()
        _, store, _ = run(monkeypatch, [energy], countries=("KR",), table=table, s3=s3)
        assert store.get_ingest_latest("energy_policy")["hashes"] == {"KR": sha}
        c2, store2, _ = run(monkeypatch, [energy], countries=("KR",), table=table, s3=s3)
        assert c2.runs["energy_policy"].n_docs == 0  # 해시 복원 → 재생성 없음
        assert store2.get_ingest_latest("energy_policy")["hashes"] == {"KR": sha}

    def test_doc_text_from_module_is_saved_to_s3(self, monkeypatch):
        doc_id = "1" * 12

        def collect_docs(countries, ctx):
            doc = Doc(type="cb_stance", iso="KR", date="2026-09-18", id=doc_id,
                      title_ko="한국은행 결정문", summary_ko="요약", source_url="https://bok.test",
                      source_name="한국은행", payload={"stance_score": 0.0},
                      ai_generated=True, quotes=["원문 인용"], confidence=0.7)
            ctx.extra.setdefault("doc_texts", {})[f"cb_stance/KR/{doc_id}"] = "성명 원문 평문"
            return [doc]

        cb = fake_module("cb_statements", obs=[], docs=collect_docs)
        s3 = FakeS3()
        _, store, _ = run(monkeypatch, [cb], countries=("KR",), s3=s3)
        key = f"macro/docs/cb_stance/KR/2026-09-18_{doc_id}.md"
        assert s3.objects[key]["Body"].decode("utf-8") == "성명 원문 평문"
        assert store.latest_doc("cb_stance", "KR")["s3_key"] == key


class TestDumpLoad:
    def test_dump_and_load_roundtrip_via_cli(self, monkeypatch, tmp_path):
        bis = fake_module("bis", obs=[
            _obs("policy_rate", "KR", "D", "2026-09-18", 2.25, "bis"),
            _obs("house_price_index", "KR", "Q", "2026-Q2", 150.123456, "bis"),
        ])
        monkeypatch.setattr(collect, "SOURCE_MODULES", {"bis": bis})
        dump1 = tmp_path / "d1.json"
        code = collect.main(["--dry-run", "--force", "--sources", "bis", "--countries", "KR",
                             "--since", "2026-01-01", "--dump", str(dump1), "--log-level", "WARNING"])
        assert code == 0 and dump1.exists()
        raw = json.loads(dump1.read_text(encoding="utf-8"))
        pks = {it["pk"] for it in raw["table"]}
        assert "OBS#policy_rate#KR" in pks and "LATEST#policy_rate" in pks and "INGEST#bis" in pks
        assert "SNAPSHOT#KR" in pks and "SERIES#policy_rate" in pks

        table = FakeTable()
        n = collect.load_state(dump1, table, FakeS3())
        assert n == len(raw["table"])
        restored = MacroStore(table).get_observation("house_price_index", "KR", "Q", "2026-Q2")
        assert restored["value"] == pytest.approx(150.123456)

        # --load 위에 이어서 수집: 기존 항목 유지 + 새 관측 추가
        bis2 = fake_module("bis", obs=[_obs("policy_rate", "KR", "D", "2026-09-19", 2.0, "bis")])
        monkeypatch.setattr(collect, "SOURCE_MODULES", {"bis": bis2})
        dump2 = tmp_path / "d2.json"
        code2 = collect.main(["--dry-run", "--force", "--sources", "bis", "--countries", "KR",
                              "--since", "2026-01-01", "--load", str(dump1), "--dump", str(dump2),
                              "--log-level", "WARNING"])
        assert code2 == 0
        table2 = FakeTable()
        collect.load_state(dump2, table2, FakeS3())
        store2 = MacroStore(table2)
        assert [r["period"] for r in store2.query_series("policy_rate", "KR", "D")] == ["2026-09-18", "2026-09-19"]
        assert store2.get_observation("house_price_index", "KR", "Q", "2026-Q2") is not None

    def test_cli_defaults(self):
        opts = collect.options_from_args(collect.parse_args(["--dry-run"]))
        assert opts.no_llm is True and opts.sources == list(collect.DEFAULT_SOURCES) and opts.countries == []
        opts2 = collect.options_from_args(collect.parse_args(["--dry-run", "--llm", "--sources", "bis,yahoo"]))
        assert opts2.no_llm is False and opts2.sources == ["bis", "yahoo"]
        opts3 = collect.options_from_args(collect.parse_args([]))
        assert opts3.no_llm is False and opts3.dry_run is False

    def test_unknown_source_or_country_returns_2(self, monkeypatch):
        assert collect.main(["--dry-run", "--sources", "nope", "--log-level", "ERROR"]) == 2
        assert collect.main(["--dry-run", "--countries", "XX", "--log-level", "ERROR"]) == 2
