"""Unit tests for the race-time forecast."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from runlog.analyze import forecast, metrics
from runlog.db import store
from runlog.domain import Activity, ActivityRecord, SourceId, StreamPoint


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(761.0, "12:41"), (3730.0, "1:02:10"), (725.4, "12:05"), (59.6, "1:00")],
)
def test_format_race_time(seconds: float, expected: str) -> None:
    assert forecast.format_race_time(seconds) == expected


def test_weekly_minima_keeps_fastest_per_week() -> None:
    # Two runs in the week of Mon Jun 1, one in the week of Mon Jun 8.
    points = [
        (date(2026, 6, 2), 820.0),
        (date(2026, 6, 4), 800.0),  # faster same week
        (date(2026, 6, 9), 810.0),
    ]
    assert forecast._weekly_minima(points) == [
        (date(2026, 6, 1), 800.0),
        (date(2026, 6, 8), 810.0),
    ]


def _series(start: date, values: list[float]) -> list[tuple[date, float]]:
    return [(start + timedelta(weeks=i), v) for i, v in enumerate(values)]


def test_trend_projection_is_ordered_and_floored() -> None:
    # Eight improving weeks (800 -> 760); race four weeks past the last sample.
    series = _series(date(2026, 5, 4), [800, 795, 790, 785, 780, 775, 770, 760])
    today = date(2026, 6, 22)  # the last sample week
    race_day = date(2026, 7, 20)
    result = forecast._forecast_from_series(series, race_day, 3000.0, today)

    assert result is not None
    assert result.method == "trend"
    assert result.n_weeks == 8
    floor = 0.95 * 760.0
    assert result.predicted_s >= floor
    assert result.predicted_s <= result.current_best_s  # still improving
    assert result.ci_low_s is not None and result.ci_high_s is not None
    assert result.ci_low_s <= result.ci_high_s


def test_fallback_when_too_few_weeks() -> None:
    series = _series(date(2026, 6, 1), [800, 790, 785])  # 3 weeks < min
    result = forecast._forecast_from_series(
        series, date(2026, 7, 20), 3000.0, date(2026, 6, 15)
    )
    assert result is not None
    assert (result.method, result.ci_low_s, result.predicted_s) == (
        "riegel-current",
        None,
        785.0,  # the current best race-equivalent
    )


def test_clamp_floor_on_steep_slope() -> None:
    # Six weeks dropping steeply (with a little wiggle so the fit isn't perfectly
    # collinear), race far in the future -> raw projection would go well below
    # the floor and must be clamped to 0.95 x current best.
    values = [900.0, 861.0, 820.0, 781.0, 740.0, 699.0]
    series = _series(date(2026, 4, 6), values)
    result = forecast._forecast_from_series(
        series, date(2026, 12, 1), 3000.0, date(2026, 5, 11)
    )
    assert result is not None
    assert result.method == "trend"
    assert result.predicted_s == pytest.approx(0.95 * min(values))


def test_race_forecast_none_without_stream_efforts() -> None:
    conn = store.connect(":memory:")  # type: ignore[arg-type]
    store.init_db(conn)
    store.store_record(
        conn,
        ActivityRecord(
            activity=Activity(
                source="strava",
                source_id=SourceId("no-stream"),
                sport_type="Run",
                start_time_utc=datetime(2026, 6, 1, 7, tzinfo=UTC),
                distance_m=5000.0,
                moving_s=1500,
                avg_pace_s_per_km=300.0,
            )
        ),
    )
    runs = metrics.canonical_run_activities(conn)
    assert (
        forecast.race_forecast(conn, runs, date(2026, 8, 30), 3000.0, date(2026, 6, 2))
        is None
    )


def test_race_equivalent_series_riegels_and_buckets() -> None:
    conn = store.connect(":memory:")  # type: ignore[arg-type]
    store.init_db(conn)
    # One 1 km run at 4 m/s -> best 1k = 250 s; Riegel to 3 km = 250*3^1.06.
    speed = 4.0
    stream = tuple(
        StreamPoint(offset_s=i, distance_m=speed * i, velocity_mps=speed)
        for i in range(251)
    )
    store.store_record(
        conn,
        ActivityRecord(
            activity=Activity(
                source="strava",
                source_id=SourceId("k1"),
                sport_type="Run",
                start_time_utc=datetime(2026, 6, 2, 7, tzinfo=UTC),
                distance_m=1000.0,
                moving_s=250,
                avg_pace_s_per_km=250.0,
            ),
            stream=stream,
        ),
    )
    runs = metrics.canonical_run_activities(conn)
    series = forecast.race_equivalent_series(conn, runs, 3000.0, date(2026, 6, 8))
    assert len(series) == 1
    assert series[0][0] == date(2026, 6, 1)
    assert series[0][1] == pytest.approx(250.0 * 3.0**1.06)
