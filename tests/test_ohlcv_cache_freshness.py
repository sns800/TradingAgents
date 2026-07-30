"""[모듈 개요] 당일 OHLCV 캐시가 오래된(stale) 스냅샷을 하루 종일 제공하지 않는지 검증하는 테스트 (#1150).

캐시 파일은 날짜 단위로 키가 매겨지므로, 당일 봉(bar)이 확정되기 전에 시작된
실행의 캐시가 이후 모든 실행에서 재사용되어 오래된 종가(close)가 기술적 분석에
흘러들 수 있었다. 당일 요청에서는 두 경우가 중요하다: 봉이 아예 없거나, 존재하지만
아직 진행 중인 경우(야후는 장중에 부분적인 일봉 캔들을 제공한다).
갱신(refresh)은 TTL로 제한되어 반복 실행이 공급자(vendor)를 과도하게 호출하지 못한다.
"""
from __future__ import annotations

import os
import time

import pandas as pd
import pytest

import tradingagents.dataflows.stockstats_utils as su

TODAY = pd.Timestamp("2026-07-18")
STALE = su.OHLCV_CACHE_TTL_SECONDS + 60


def _write(tmp_path, name="cache.csv", age_seconds=0.0, last_date="2026-07-17"):
    f = tmp_path / name
    pd.DataFrame({"Date": [last_date], "Close": [1.0]}).to_csv(f, index=False)
    if age_seconds:
        old = time.time() - age_seconds
        os.utime(f, (old, old))
    return str(f)


@pytest.mark.unit
def test_current_day_cache_past_ttl_is_refreshed(tmp_path):
    """TTL이 지난 당일 캐시는 다시 가져오도록(refetch) 판단하는지 검증하는 테스트."""
    # 봉이 없고(행이 어제까지만 있음) 파일이 TTL보다 오래됨 -> 다시 가져와야 한다.
    assert su._needs_same_day_refresh(_write(tmp_path, age_seconds=STALE), TODAY, TODAY) is True


@pytest.mark.unit
def test_partial_current_day_bar_is_still_refreshed(tmp_path):
    """당일 행이 있어도 TTL이 지났으면 갱신하는지 검증하는 테스트."""
    # 오늘 행이 존재하지만 Close가 아직 종가가 아닌 진행 중인 캔들일 수 있다.
    # 행 검사로는 구분할 수 없으므로 TTL이 기준이 된다.
    f = _write(tmp_path, age_seconds=STALE, last_date="2026-07-18")
    assert su._needs_same_day_refresh(f, TODAY, TODAY) is True


@pytest.mark.unit
def test_recent_cache_is_not_refetched(tmp_path):
    """방금 기록된 신선한 캐시는 다시 가져오지 않는지 검증하는 테스트."""
    # 방금 기록됨: 공급자를 과도하게 호출하지 않는다 (주말/휴일 보호 장치).
    assert su._needs_same_day_refresh(_write(tmp_path), TODAY, TODAY) is False


@pytest.mark.unit
def test_historical_request_always_uses_cache(tmp_path):
    """과거 날짜 요청은 캐시 파일이 아무리 오래돼도 항상 캐시를 사용하는지 검증하는 테스트."""
    # 과거 날짜는 불변(immutable): 파일이 아무리 오래돼도 다시 가져오지 않는다.
    past = pd.Timestamp("2026-05-01")
    f = _write(tmp_path, age_seconds=STALE, last_date="2026-04-30")
    assert su._needs_same_day_refresh(f, past, TODAY) is False


@pytest.mark.unit
def test_load_ohlcv_refetches_stale_same_day_cache(tmp_path, monkeypatch):
    """엔드투엔드(end-to-end): 헬퍼가 실제로 load_ohlcv의 캐시 분기에 연결돼 있는지 검증하는 테스트.

    이 테스트가 없으면, 헬퍼가 실제 코드 경로에서 전혀 호출되지 않더라도
    위의 단위 테스트들은 여전히 통과할 수 있다.
    """
    monkeypatch.setattr(su, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
    monkeypatch.setattr(su.pd.Timestamp, "today", staticmethod(lambda: TODAY))

    # load_ohlcv가 찾을 캐시 파일을 TTL이 지난 상태로 미리 심어 둔다.
    start = (TODAY - pd.DateOffset(years=5)).strftime("%Y-%m-%d")
    end = (TODAY + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    cache_file = tmp_path / f"AAPL-YFin-data-{start}-{end}.csv"
    pd.DataFrame({"Date": ["2026-07-17"], "Close": [100.0]}).to_csv(cache_file, index=False)
    old = time.time() - STALE
    os.utime(cache_file, (old, old))

    calls = []

    def _fake_download(*a, **k):
        calls.append(1)
        return pd.DataFrame(
            {"Date": pd.to_datetime(["2026-07-17", "2026-07-18"]), "Close": [100.0, 222.0]}
        ).set_index("Date")

    monkeypatch.setattr(su.yf, "download", _fake_download)

    out = su.load_ohlcv("AAPL", TODAY.strftime("%Y-%m-%d"))

    assert calls, "stale same-day cache must trigger a refetch"
    assert 222.0 in out["Close"].values, "refreshed close must reach the caller"


@pytest.mark.unit
def test_load_ohlcv_reuses_fresh_same_day_cache(tmp_path, monkeypatch):
    """신선한 당일 캐시가 있으면 load_ohlcv가 다운로드하지 않는지 검증하는 테스트."""
    # 앞 테스트의 반대 상황: 신선한 캐시는 절대 다운로드를 유발하면 안 된다.
    monkeypatch.setattr(su, "get_config", lambda: {"data_cache_dir": str(tmp_path)})
    monkeypatch.setattr(su.pd.Timestamp, "today", staticmethod(lambda: TODAY))

    start = (TODAY - pd.DateOffset(years=5)).strftime("%Y-%m-%d")
    end = (TODAY + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    cache_file = tmp_path / f"AAPL-YFin-data-{start}-{end}.csv"
    pd.DataFrame({"Date": ["2026-07-18"], "Close": [100.0]}).to_csv(cache_file, index=False)

    def _fail_download(*a, **k):
        raise AssertionError("fresh cache must not refetch")

    monkeypatch.setattr(su.yf, "download", _fail_download)
    su.load_ohlcv("AAPL", TODAY.strftime("%Y-%m-%d"))
