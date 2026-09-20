# ============================================================
# [모듈 개요] webui/macro/aggregate.py 집계 규칙 테스트 (CONTRACT.md 6장)
#
# 기간 유틸·저빈도 집계(기말/평균)·전년비·지수화·연료 의존도·여론조사 평균·
# 개요용 최신값/순위를 손으로 검산 가능한 작은 예로 검증한다. 외부 I/O 없음.
# ============================================================
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "webui"))

from macro import aggregate as agg  # noqa: E402
from macro.schema import Doc, Observation  # noqa: E402

pytestmark = pytest.mark.unit

TODAY = date(2026, 9, 20)


def obs(
    period: str,
    value: float | None,
    *,
    freq: str = "M",
    indicator: str = "cpi_index",
    iso: str = "KR",
    unit: str = "index",
    payload: dict | None = None,
    flags: list[str] | None = None,
) -> Observation:
    return Observation(
        indicator=indicator,
        iso=iso,
        freq=freq,
        period=period,
        value=value,
        payload=payload,
        unit=unit,
        source="oecd",
        series_id="TEST",
        source_url="https://example.test/series",
        method="테스트 입력",
        vintage="2026-09-01",
        flags=flags or [],
    )


# ------------------------------------------------------------------
# 기간 유틸
# ------------------------------------------------------------------
def test_period_sort_key로_시간순_정렬():
    months = ["2026-01", "2025-12", "2026-02"]
    assert sorted(months, key=lambda p: agg.period_sort_key("M", p)) == [
        "2025-12",
        "2026-01",
        "2026-02",
    ]
    quarters = ["2026-Q1", "2025-Q4", "2026-Q3"]
    assert sorted(quarters, key=lambda p: agg.period_sort_key("Q", p)) == [
        "2025-Q4",
        "2026-Q1",
        "2026-Q3",
    ]
    assert agg.period_sort_key("D", "2026-09-19") == (2026, 9, 19)
    assert agg.period_sort_key("Y", "2025") == (2025, 0, 0)


def test_month_to_quarter():
    assert agg.month_to_quarter("2026-08") == "2026-Q3"
    assert agg.month_to_quarter("2026-01") == "2026-Q1"
    assert agg.month_to_quarter("2026-03") == "2026-Q1"
    assert agg.month_to_quarter("2026-12") == "2026-Q4"


def test_period_to_year():
    assert agg.period_to_year("2026-08") == "2026"
    assert agg.period_to_year("2026-Q3") == "2026"
    assert agg.period_to_year("2026-09-19") == "2026"
    assert agg.period_to_year("2026") == "2026"


def test_previous_period():
    assert agg.previous_period("M", "2026-01") == "2025-12"
    assert agg.previous_period("M", "2026-08", 12) == "2025-08"
    assert agg.previous_period("Q", "2026-Q1") == "2025-Q4"
    assert agg.previous_period("Q", "2026-Q3", 4) == "2025-Q3"
    assert agg.previous_period("Y", "2026", 1) == "2025"
    assert agg.previous_period("D", "2026-03-01", 1) == "2026-02-28"
    assert agg.previous_period("W", "2026-09-20", 1) == "2026-09-13"
    assert agg.previous_period("M", "2026-08", -1) == "2026-09"  # 음수는 이후 기간


def test_periods_between():
    assert agg.periods_between("M", "2025-11", "2026-02") == [
        "2025-11",
        "2025-12",
        "2026-01",
        "2026-02",
    ]
    assert agg.periods_between("Q", "2025-Q3", "2026-Q1") == ["2025-Q3", "2025-Q4", "2026-Q1"]
    assert agg.periods_between("Y", "2024", "2026") == ["2024", "2025", "2026"]
    assert agg.periods_between("M", "2026-05", "2026-01") == []  # 역순은 빈 목록


def test_period_end_date():
    assert agg.period_end_date("M", "2026-02") == date(2026, 2, 28)
    assert agg.period_end_date("Q", "2026-Q3") == date(2026, 9, 30)
    assert agg.period_end_date("Y", "2026") == date(2026, 12, 31)


# ------------------------------------------------------------------
# 저빈도 집계
# ------------------------------------------------------------------
def test_일별을_월별_기말값으로():
    daily = [
        obs("2026-07-30", 2.50, freq="D", indicator="policy_rate", unit="%"),
        obs("2026-07-31", 2.75, freq="D", indicator="policy_rate", unit="%"),
        obs("2026-08-03", 2.75, freq="D", indicator="policy_rate", unit="%"),
        obs("2026-08-31", 3.00, freq="D", indicator="policy_rate", unit="%"),
    ]
    out = agg.to_lower_freq(daily, "M", "last", today=TODAY)
    assert [(o.period, o.value) for o in out] == [("2026-07", 2.75), ("2026-08", 3.00)]
    assert out[0].freq == "M"
    assert out[0].indicator == "policy_rate"
    assert out[0].method == "월값 = 월 마지막 관측일(2026-07-31) 값"
    assert out[1].flags == []  # 8월은 달력상 완결


def test_월별을_분기_기말값으로_진행중_분기는_partial():
    months = [obs(p, v) for p, v in [("2026-04", 100.0), ("2026-05", 101.0), ("2026-06", 102.0)]]
    months += [obs("2026-07", 103.0), obs("2026-08", 104.0)]
    out = agg.to_lower_freq(months, "Q", "last", today=TODAY)
    assert [(o.period, o.value) for o in out] == [("2026-Q2", 102.0), ("2026-Q3", 104.0)]
    assert out[0].method == "분기값 = 분기 마지막 월(2026-06) 월값"
    assert out[0].flags == []
    assert "partial_period" in out[1].flags
    assert out[1].method.endswith("진행 중 기간")


def test_월별을_연평균으로():
    months = [obs(f"2025-{m:02d}", float(m)) for m in range(1, 13)]
    out = agg.to_lower_freq(months, "Y", "mean", today=TODAY)
    assert len(out) == 1
    assert out[0].period == "2025"
    assert out[0].value == pytest.approx(6.5)  # (1+..+12)/12
    assert out[0].method == "연평균 (12개월)"
    assert out[0].flags == []


def test_분기를_연_기말값으로_진행중_연도는_partial():
    quarters = [
        obs(p, v, freq="Q", indicator="house_price_index")
        for p, v in [("2025-Q3", 90.0), ("2025-Q4", 95.0), ("2026-Q1", 97.0), ("2026-Q2", 99.0)]
    ]
    out = agg.to_lower_freq(quarters, "Y", "last", today=TODAY)
    assert [(o.period, o.value) for o in out] == [("2025", 95.0), ("2026", 99.0)]
    assert out[0].method == "연값 = 연 마지막 분기(2025-Q4) 분기값"
    assert out[0].flags == []
    assert "partial_period" in out[1].flags


def test_합계_집계와_플래그_승계():
    months = [obs("2025-01", 10.0), obs("2025-02", 20.0, flags=["fallback_source"])]
    out = agg.to_lower_freq(months, "Q", "sum", today=TODAY)
    assert out[0].value == 30.0
    assert out[0].method == "분기합계 (2개월)"
    assert out[0].flags == ["fallback_source"]  # 마지막(대표) 관측치의 플래그를 승계


def test_잘못된_집계_요청은_ValueError():
    months = [obs("2026-01", 1.0)]
    with pytest.raises(ValueError, match="집계는 지원하지 않는다"):
        agg.to_lower_freq(months, "M", "last", today=TODAY)  # 같은 빈도
    with pytest.raises(ValueError, match="지원하지 않는 집계 방식"):
        agg.to_lower_freq(months, "Y", "none", today=TODAY)
    with pytest.raises(ValueError, match="같은 indicator"):
        agg.to_lower_freq([obs("2026-01", 1.0), obs("2026-02", 2.0, iso="US")], "Q", "last")
    with pytest.raises(ValueError, match="복합값"):
        agg.to_lower_freq(
            [obs("2026-01", None, payload={"items": []}), obs("2026-02", 2.0)], "Q", "last"
        )
    assert agg.to_lower_freq([], "Y", "last") == []


# ------------------------------------------------------------------
# 전년비 / 지수
# ------------------------------------------------------------------
def test_월별_전년비는_12개월_시차():
    months = [obs("2025-07", 100.0), obs("2025-08", 200.0), obs("2026-07", 105.0)]
    months += [obs("2026-08", 207.0)]
    out = agg.yoy(months, "cpi_yoy")
    assert [(o.period, round(o.value, 4)) for o in out] == [
        ("2026-07", 5.0),
        ("2026-08", 3.5),
    ]
    assert out[0].indicator == "cpi_yoy"
    assert out[0].unit == "%"
    assert out[0].source == "derived"
    assert out[0].flags == ["derived"]
    assert out[0].method == "전년비 = (2026-07 / 2025-07 − 1) × 100"


def test_전년비_기본_지표명과_분기_시차():
    quarters = [
        obs(p, v, freq="Q", indicator="house_price_index")
        for p, v in [("2025-Q2", 80.0), ("2026-Q2", 100.0)]
    ]
    out = agg.yoy(quarters)
    assert out[0].indicator == "house_price_yoy"  # *_index -> *_yoy
    assert out[0].value == pytest.approx(25.0)
    assert out[0].period == "2026-Q2"


def test_전년비_partial_플래그_승계와_일별_금지():
    months = [obs("2025-08", 100.0), obs("2026-08", 110.0, flags=["partial_period"])]
    out = agg.yoy(months, "cpi_yoy")
    assert out[0].flags == ["derived", "partial_period"]
    daily = [obs("2026-09-19", 1.0, freq="D", indicator="policy_rate", unit="%")]
    with pytest.raises(ValueError, match="전년비를 계산하지 않는다"):
        agg.yoy(daily, "policy_rate_yoy")


def test_index100_기준값이_있을_때():
    months = [obs("2020-01", 80.0), obs("2026-08", 120.0)]
    out = agg.index100(months, "2020-01")
    assert [round(o.value, 4) for o in out] == [100.0, 150.0]
    assert out[0].unit == "index"
    assert out[0].method == "지수 = 값 / 기준값(2020-01) × 100"


def test_index100_기준값이_없으면_최근접_이전_관측을_쓴다():
    months = [obs("2019-12", 50.0), obs("2020-03", 75.0)]
    out = agg.index100(months, "2020-01")
    assert [round(o.value, 2) for o in out] == [100.0, 150.0]
    assert "2019-12" in out[0].method
    assert "최근접 이전 관측" in out[0].method
    with pytest.raises(ValueError, match="기준 기간"):
        agg.index100([obs("2021-01", 10.0)], "2020-01")


def test_fx_value_index는_환율의_역수_지수():
    fx = [
        obs(p, v, indicator="fx_usd", unit="lcu_per_usd")
        for p, v in [("2025-01", 1300.0), ("2025-02", 1400.0), ("2025-03", 1250.0)]
    ]
    out = agg.fx_value_index(fx, "2025-01")
    assert out[0].value == pytest.approx(100.0)
    assert out[1].value == pytest.approx(1300.0 / 1400.0 * 100.0)  # 환율↑ = 통화 약세
    assert out[1].value < 100.0
    assert out[2].value == pytest.approx(104.0)  # 1300/1250*100
    assert out[0].indicator == "fx_value_index"
    assert out[0].unit == "index"
    assert out[0].flags == ["derived"]
    assert out[0].method.startswith("통화가치지수 = 기준환율(2025-01)")


# ------------------------------------------------------------------
# 연료 의존도
# ------------------------------------------------------------------
def test_fuel_dependency():
    assert agg.fuel_dependency(20.0, 100.0) == pytest.approx(80.0)
    assert agg.fuel_dependency(100.0, 100.0) == pytest.approx(0.0)
    assert agg.fuel_dependency(150.0, 100.0) == pytest.approx(0.0)  # 순수출국은 0으로 하한
    assert agg.fuel_dependency(0.0, 100.0) == pytest.approx(100.0)
    assert agg.fuel_dependency(10.0, 0.0) is None
    assert agg.fuel_dependency(10.0, -5.0) is None
    assert agg.fuel_dependency(None, 100.0) is None
    assert agg.fuel_dependency(10.0, None) is None


# ------------------------------------------------------------------
# 여론조사 평균
# ------------------------------------------------------------------
def poll(
    poll_date: str,
    results: dict[str, float],
    sample: int | None,
    approval: float | None = None,
) -> Doc:
    return Doc(
        type="poll",
        iso="KR",
        date=poll_date,
        id="0123456789ab",
        title_ko="테스트 조사",
        summary_ko="테스트",
        source_url="https://example.test/poll",
        source_name="테스트기관",
        payload={
            "pollster": "테스트기관",
            "fieldwork_start": poll_date,
            "fieldwork_end": poll_date,
            "sample_size": sample,
            "results": results,
            "gov_approval": approval,
            "method": None,
        },
    )


def test_poll_of_polls_가중_평균():
    polls = [
        # age 0 → 최근성 1.0, 표본 1000 → 가중치 1000
        poll("2026-09-20", {"여당": 40.0, "야당": 35.0}, 1000, approval=45.0),
        # age 15 → 최근성 0.75, 표본 2000 → 가중치 1500
        poll("2026-09-05", {"여당": 30.0, "야당": 45.0}, 2000),
        # 창(30일) 밖 → 제외
        poll("2026-08-01", {"여당": 10.0, "야당": 80.0}, 5000),
    ]
    out = agg.poll_of_polls(polls, "2026-09-20", ruling_party="여당", window_days=30)
    assert out["n_polls"] == 2
    assert out["results"] == {"여당": 34.0, "야당": 41.0}
    assert out["ruling_party"] == "여당"
    assert out["ruling_pct"] == 34.0
    assert out["leader_party"] == "야당"
    assert out["leader_pct"] == 41.0
    assert out["gov_approval"] == 45.0  # 지지율을 보고한 조사만 평균
    assert out["asof"] == "2026-09-20"
    assert out["window_days"] == 30


def test_poll_of_polls_표본_미기재는_1000으로_가정():
    polls = [
        poll("2026-09-20", {"A": 50.0}, None),
        poll("2026-09-20", {"A": 30.0}, 3000),
    ]
    out = agg.poll_of_polls(polls, "2026-09-20", ruling_party="A")
    # (1000*50 + 3000*30) / 4000 = 35.0
    assert out["results"] == {"A": 35.0}


def test_poll_of_polls_창_안에_조사가_없으면_None():
    polls = [poll("2026-01-01", {"A": 50.0}, 1000)]
    assert agg.poll_of_polls(polls, "2026-09-20") is None
    assert agg.poll_of_polls([], "2026-09-20") is None
    # 미래 조사도 제외
    assert agg.poll_of_polls([poll("2026-09-25", {"A": 1.0}, 1000)], "2026-09-20") is None


# ------------------------------------------------------------------
# 개요(LATEST)
# ------------------------------------------------------------------
def test_latest_by_country_순위와_변화():
    data = {
        "KR": [
            obs("2026-07", 3.0, indicator="policy_rate", unit="%"),
            obs("2026-08", 3.5, indicator="policy_rate", unit="%"),
        ],
        "US": [obs("2026-08", 4.5, indicator="policy_rate", unit="%", iso="US")],
        "JP": [
            obs("2026-07", 0.25, indicator="policy_rate", unit="%", iso="JP"),
            obs("2026-08", 0.5, indicator="policy_rate", unit="%", iso="JP"),
        ],
    }
    out = agg.latest_by_country(data)
    assert out["KR"]["value"] == 3.5
    assert out["KR"]["period"] == "2026-08"
    assert out["KR"]["prev_value"] == 3.0
    assert out["KR"]["prev_period"] == "2026-07"
    assert out["KR"]["change"] == pytest.approx(0.5)
    assert out["KR"]["change_pct"] == pytest.approx((3.5 / 3.0 - 1) * 100)
    assert out["US"]["prev_value"] is None
    assert out["US"]["change"] is None
    assert (out["US"]["rank"], out["KR"]["rank"], out["JP"]["rank"]) == (1, 2, 3)
    assert {row["n"] for row in out.values()} == {3}
    assert agg.median([row["value"] for row in out.values()]) == 3.5


def test_latest_by_country_복합값은_순위에서_제외():
    data = {
        "KR": [obs("2025", None, freq="Y", indicator="elec_mix", payload={"items": [1]})],
        "US": [obs("2025", 10.0, freq="Y", indicator="elec_mix", iso="US")],
    }
    out = agg.latest_by_country(data)
    assert out["KR"]["value"] is None
    assert out["KR"]["rank"] is None
    assert out["KR"]["payload"] == {"items": [1]}
    assert out["US"]["rank"] == 1
    assert out["US"]["n"] == 1


def test_median():
    assert agg.median([3.0, 1.0, 2.0]) == 2.0
    assert agg.median([4.0, 1.0, 3.0, 2.0]) == 2.5
    assert agg.median([None, 5.0]) == 5.0
    assert agg.median([None, None]) is None
    assert agg.median([]) is None
