"""[모듈 개요] stockstats_utils가 `Date`가 아닌 날짜 컬럼 이름을 허용하는지 검증하는 테스트 (#890).

다운로드된 데이터프레임의 날짜 컬럼이 `Date` 대신 `index`나 `Datetime`으로
오는 경우를 방어한다. 방어하지 않으면 모든 기술 지표(indicator)가 조용히
누락된다.
"""

from __future__ import annotations

import pandas as pd
import pytest

from tradingagents.dataflows import stockstats_utils as su


def _ohlcv(date_col: str) -> pd.DataFrame:
    """날짜 컬럼 이름이 `date_col`인 OHLCV 데이터프레임을 생성한다."""
    dates = pd.bdate_range("2026-04-01", periods=10)
    return pd.DataFrame({
        date_col: dates,
        "Open": [100.0 + i for i in range(10)],
        "High": [101.0 + i for i in range(10)],
        "Low": [99.0 + i for i in range(10)],
        "Close": [100.5 + i for i in range(10)],
        "Volume": [1_000_000 + i for i in range(10)],
    })


@pytest.mark.unit
class TestEnsureDateColumn:
    def test_renames_index_column(self):
        """`index` 컬럼이 `Date`로 이름이 바뀌는지 검증하는 테스트."""
        out = su._ensure_date_column(_ohlcv("index"))
        assert "Date" in out.columns and "index" not in out.columns

    def test_renames_datetime_and_date_variants(self):
        """`Datetime`, `date` 같은 변형 이름도 `Date`로 정규화되는지 검증하는 테스트."""
        assert "Date" in su._ensure_date_column(_ohlcv("Datetime")).columns
        assert "Date" in su._ensure_date_column(_ohlcv("date")).columns

    def test_leaves_existing_date_untouched(self):
        """이미 `Date` 컬럼이 있으면 원본을 그대로 반환하는지 검증하는 테스트."""
        df = _ohlcv("Date")
        assert su._ensure_date_column(df) is df  # 아무것도 하지 않는(no-op) 단락 처리

    def test_no_datelike_column_is_left_alone(self):
        """날짜 형태의 컬럼이 아예 없으면 아무것도 바꾸지 않는지 검증하는 테스트."""
        df = pd.DataFrame({"Close": [1, 2, 3]})
        out = su._ensure_date_column(df)
        assert "Date" not in out.columns  # 바꿀 대상이 없음; 호출자가 처리한다


@pytest.mark.unit
class TestCleanDataframeAcrossVersions:
    def test_clean_handles_index_column(self):
        """`Date` 대신 `index` 컬럼을 가진 데이터프레임도 날짜가 파싱된 사용 가능한
        형태로 정제되는지 검증하는 테스트 (예전엔 KeyError: 'Date' 발생)."""
        cleaned = su._clean_dataframe(_ohlcv("index"))
        assert "Date" in cleaned.columns
        assert pd.api.types.is_datetime64_any_dtype(cleaned["Date"])
        assert len(cleaned) == 10

    def test_clean_handles_legacy_date_column(self):
        """기존 `Date` 컬럼 형식도 문제없이 정제되는지 검증하는 테스트."""
        cleaned = su._clean_dataframe(_ohlcv("Date"))
        assert len(cleaned) == 10

    def test_indicators_compute_after_index_rename(self):
        """날짜 컬럼이 `index`로 온 데이터프레임에서도 지표별 오류 없이
        stockstats가 기술 지표를 계산하는지 검증하는 테스트."""
        from stockstats import wrap
        cleaned = su._clean_dataframe(_ohlcv("index"))
        df = wrap(cleaned)
        df["close_5_sma"]  # 지표 계산을 트리거한다
        assert "close_5_sma" in df.columns
        assert df["close_5_sma"].notna().any()
