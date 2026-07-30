# 이 파일은 결정론적 시장 데이터 검증 스냅샷(snapshot) 생성을 검증하는
# 테스트 모음입니다. 미래 데이터 배제, 주말 처리, 데이터 없음 오류,
# 조회 기간 상한 등을 확인합니다.
"""결정론적 시장 데이터 검증 스냅샷(snapshot) 테스트 (#830/#881)."""

from __future__ import annotations

import pandas as pd
import pytest

import tradingagents.dataflows.market_data_validator as validator


def _sample_ohlcv() -> pd.DataFrame:
    dates = pd.bdate_range("2026-04-01", "2026-05-20")
    closes = [100 + i for i in range(len(dates))]
    return pd.DataFrame({
        "Date": dates,
        "Open": [c - 0.5 for c in closes],
        "High": [c + 1.0 for c in closes],
        "Low": [c - 1.0 for c in closes],
        "Close": closes,
        "Volume": [1_000_000 + i for i in range(len(dates))],
    })


@pytest.mark.unit
class TestVerifiedSnapshot:
    """검증된 시장 스냅샷 생성 로직을 검증하는 테스트 묶음."""

    def test_excludes_future_rows(self, monkeypatch):
        """분석 날짜 이후의 미래 데이터 행이 스냅샷에서 제외되는지 검증하는 테스트."""
        data = pd.concat([
            _sample_ohlcv(),
            pd.DataFrame({"Date": [pd.Timestamp("2026-06-01")], "Open": [999.0],
                          "High": [999.0], "Low": [999.0], "Close": [999.0], "Volume": [999]}),
        ], ignore_index=True)
        monkeypatch.setattr(validator, "load_ohlcv", lambda s, d: data)

        snap = validator.build_verified_market_snapshot("COF", "2026-05-13")
        assert "Verified market data snapshot for COF" in snap
        assert "Requested analysis date: 2026-05-13" in snap
        assert "Latest trading row used: 2026-05-13" in snap
        assert "999.00" not in snap          # 미래 행이 제외됨
        assert "boll_lb" in snap             # 기술 지표가 포함됨

    def test_uses_previous_trading_day_when_date_is_weekend(self, monkeypatch):
        """주말 날짜로 요청하면 직전 거래일 데이터를 사용하는지 검증하는 테스트."""
        monkeypatch.setattr(validator, "load_ohlcv", lambda s, d: _sample_ohlcv())
        # 2026-05-16은 토요일이므로 최신 행은 금요일인 2026-05-15여야 함
        snap = validator.build_verified_market_snapshot("COF", "2026-05-16")
        assert "Latest trading row used: 2026-05-15" in snap
        assert "Recent verified closes" in snap

    def test_raises_when_no_rows_on_or_before_date(self, monkeypatch):
        """요청 날짜 이전의 데이터가 전혀 없으면 ValueError가 발생하는지 검증하는 테스트."""
        monkeypatch.setattr(validator, "load_ohlcv", lambda s, d: _sample_ohlcv())
        with pytest.raises(ValueError):
            validator.build_verified_market_snapshot("COF", "2020-01-01")

    def test_raises_on_empty_data(self, monkeypatch):
        """빈 데이터프레임이 오면 ValueError가 발생하는지 검증하는 테스트."""
        monkeypatch.setattr(validator, "load_ohlcv", lambda s, d: pd.DataFrame())
        with pytest.raises(ValueError):
            validator.build_verified_market_snapshot("COF", "2026-05-13")

    def test_look_back_window_capped_at_30(self, monkeypatch):
        """과거 조회 기간(look_back_days)을 크게 줘도 표가 최대 30행으로 제한되는지 검증하는 테스트."""
        monkeypatch.setattr(validator, "load_ohlcv", lambda s, d: _sample_ohlcv())
        snap = validator.build_verified_market_snapshot("COF", "2026-05-20", look_back_days=999)
        # 최근 종가 표의 데이터 행은 최대 30개
        close_rows = [ln for ln in snap.splitlines() if ln.startswith("| 2026-")]
        assert 0 < len(close_rows) <= 30


@pytest.mark.unit
class TestTool:
    """LLM 도구(tool) 래퍼가 스냅샷 빌더에 위임하는지 검증하는 테스트 묶음."""

    def test_tool_delegates_to_builder(self, monkeypatch):
        """get_verified_market_snapshot 도구 호출이 빌더 함수로 위임되는지 검증하는 테스트."""
        from tradingagents.agents.utils.market_data_validation_tools import (
            get_verified_market_snapshot,
        )
        monkeypatch.setattr(validator, "load_ohlcv", lambda s, d: _sample_ohlcv())
        out = get_verified_market_snapshot.invoke(
            {"symbol": "COF", "curr_date": "2026-05-20"}
        )
        assert "Verified market data snapshot for COF" in out
