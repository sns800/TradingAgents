# 이 파일은 yfinance 데이터 조회 시 날짜 경계(boundary) 처리를 검증하는
# 테스트 모음입니다. 요청한 종료일과 당일 데이터가 결과에 빠지지 않는지 확인합니다.
"""yfinance는 ``end``를 배타적(exclusive)으로 취급하므로, 요청한 end_date(그리고
당일)가 실제로 포함되려면 하루를 더해서 요청해야 합니다.

다음 이슈들의 회귀(regression) 방지용: #986 (당일 OHLCV 누락),
#987 (요청한 end_date 행 누락).
"""
import pandas as pd
import pytest

import tradingagents.dataflows.stockstats_utils as su
import tradingagents.dataflows.y_finance as yfin
from tradingagents.dataflows.config import set_config


@pytest.mark.unit
def test_get_yfin_requests_inclusive_end(monkeypatch):
    """요청한 종료일(end_date)이 결과에 포함되도록 하루를 더해 요청하는지 검증하는 테스트 (#987)."""
    captured = {}

    class FakeTicker:
        def __init__(self, symbol):
            pass

        def history(self, start, end):
            captured["start"] = start
            captured["end"] = end
            idx = pd.to_datetime(["2025-05-08", "2025-05-09"])
            return pd.DataFrame(
                {"Open": [1.0, 2.0], "High": [1.0, 2.0], "Low": [1.0, 2.0],
                 "Close": [1.0, 2.0], "Volume": [1, 2]},
                index=idx,
            )

    monkeypatch.setattr(yfin.yf, "Ticker", FakeTicker)
    out = yfin.get_YFin_data_online("AAPL", "2025-05-01", "2025-05-09")

    # 2025-05-09가 포함되도록 end는 end_date보다 하루 뒤로 요청됩니다 (#987).
    assert captured["end"] == "2025-05-10"
    # 헤더에는 내부적으로 더한 +1일이 아닌, 사용자가 요청한 범위가 표시되어야 합니다.
    assert "to 2025-05-09" in out


@pytest.mark.unit
def test_load_ohlcv_requests_inclusive_end(monkeypatch, tmp_path):
    """당일 OHLCV(시가·고가·저가·종가·거래량) 행이 포함되도록 내일 날짜로 요청하는지 검증하는 테스트 (#986)."""
    set_config({"data_cache_dir": str(tmp_path)})
    captured = {}

    def fake_download(symbol, start, end, **kwargs):
        captured["end"] = end
        idx = pd.to_datetime([pd.Timestamp.today().normalize()])
        return pd.DataFrame(
            {"Open": [100.0], "High": [100.0], "Low": [100.0],
             "Close": [100.0], "Volume": [1]},
            index=idx,
        )

    monkeypatch.setattr(su.yf, "download", fake_download)
    today = pd.Timestamp.today().strftime("%Y-%m-%d")
    su.load_ohlcv("AAPL", today)

    expected_end = (pd.Timestamp.today() + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    assert captured["end"] == expected_end  # 내일로 요청 -> 오늘 행이 포함됨 (#986)
