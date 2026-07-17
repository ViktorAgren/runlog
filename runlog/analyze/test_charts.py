"""Smoke tests for chart rendering (Agg backend, writes real PNGs)."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime
from pathlib import Path

from runlog.analyze import charts
from runlog.analyze.anomaly import Anomaly, AnomalyReport, Direction, RedFlagDay
from runlog.analyze.metrics import (
    BucketPace,
    Heatmap,
    HrPoint,
    HrZone,
    PacePoint,
    RacePrediction,
    WeeklyLoad,
    WeeklyVolume,
)


def _wrote_png(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 0


def test_weekly_volume_chart_writes_png(tmp_path: Path) -> None:
    weekly = [
        WeeklyVolume(date(2026, 6, 1), 10.0, 2, 10.0),
        WeeklyVolume(date(2026, 6, 8), 15.0, 3, 12.5),
    ]
    assert _wrote_png(charts.weekly_volume_chart(weekly, tmp_path))


def test_monthly_by_year_chart_writes_png(tmp_path: Path) -> None:
    by_year = {2025: [5.0] * 12, 2026: [8.0] * 12}
    assert _wrote_png(charts.monthly_by_year_chart(by_year, tmp_path))


def test_pace_over_time_chart_writes_png(tmp_path: Path) -> None:
    points = [
        PacePoint(datetime(2026, 6, d, tzinfo=UTC), 300.0 + d, 5.0, 150.0)
        for d in range(1, 8)
    ]
    assert _wrote_png(charts.pace_over_time_chart(points, tmp_path))


def test_grade_adjusted_pace_chart_writes_png(tmp_path: Path) -> None:
    points = [
        PacePoint(datetime(2026, 6, d, tzinfo=UTC), 300.0 + d, 5.0, 150.0)
        for d in range(1, 8)
    ]
    assert _wrote_png(charts.grade_adjusted_pace_chart(points, tmp_path))


def test_fastest_by_bucket_chart_writes_png(tmp_path: Path) -> None:
    buckets = [
        BucketPace("<3k", 280.0, 3),
        BucketPace("3-5k", 300.0, 5),
        BucketPace("10k+", None, 0),
    ]
    assert _wrote_png(charts.fastest_by_bucket_chart(buckets, tmp_path))


def test_hr_charts_write_png(tmp_path: Path) -> None:
    hr_points = [
        HrPoint(datetime(2026, 6, d, tzinfo=UTC), 150.0, 180.0) for d in range(1, 5)
    ]
    assert _wrote_png(charts.hr_over_time_chart(hr_points, tmp_path))
    assert _wrote_png(charts.hr_histogram([120.0, 130.0, 140.0, 150.0], tmp_path))


def test_marker_chart_writes_png(tmp_path: Path) -> None:
    daily = [(date(2026, 6, d), 50.0 + d) for d in range(1, 4)]
    assert _wrote_png(
        charts.marker_chart(daily, "VO2max", "ml/kg/min", "vo2max.png", tmp_path)
    )


def test_race_prediction_chart_writes_png(tmp_path: Path) -> None:
    predictions = [
        RacePrediction("5k", 5.0, 1500.0),
        RacePrediction("10k", 10.0, 3200.0),
    ]
    assert _wrote_png(charts.race_prediction_chart(predictions, tmp_path))


def test_consistency_and_load_charts_write_png(tmp_path: Path) -> None:
    weekly = [WeeklyVolume(date(2026, 6, 1), 10.0, 3, 10.0)]
    assert _wrote_png(charts.runs_per_week_chart(weekly, tmp_path))
    zones = [
        HrZone("Z1", 60, 90, 108),
        HrZone("Z2", 120, 108, 126),
        HrZone("Z5", 30, 162, None),
    ]
    assert _wrote_png(charts.hr_zones_chart(zones, 180.0, tmp_path))
    assert _wrote_png(
        charts.training_load_chart([WeeklyLoad(date(2026, 6, 1), 42.0)], tmp_path)
    )


def test_cadence_elevation_timing_charts_write_png(tmp_path: Path) -> None:
    cadence = [(datetime(2026, 6, d, tzinfo=UTC), 82.0 + d) for d in range(1, 5)]
    assert _wrote_png(charts.cadence_chart(cadence, tmp_path))
    assert _wrote_png(charts.elevation_by_month_chart({2026: [10.0] * 12}, tmp_path))
    assert _wrote_png(
        charts.cumulative_ytd_chart({2026: [(2, 5.0), (5, 8.0)]}, tmp_path)
    )


def test_anomaly_timeline_chart_writes_png(tmp_path: Path) -> None:
    report = AnomalyReport(
        health=[
            Anomaly(date(2026, 6, 1), "resting_hr", 62.0, 50.0, 4.0, Direction.HIGH),
            Anomaly(date(2026, 6, 1), "hrv_sdnn", 30.0, 60.0, -4.0, Direction.LOW),
        ],
        red_flag_days=[RedFlagDay(date(2026, 6, 1), ("hrv_sdnn", "resting_hr"))],
        performance=[
            Anomaly(
                date(2026, 6, 5), "efficiency_factor", 5.0, 6.0, -3.0, Direction.LOW
            )
        ],
    )
    assert _wrote_png(charts.anomaly_timeline_chart(report, tmp_path))


def test_stream_charts_write_png(tmp_path: Path) -> None:
    from runlog.analyze.physiology import IntensityDistribution

    dist = IntensityDistribution(78.0, 8.0, 14.0, 5.6)
    assert _wrote_png(charts.intensity_distribution_chart(dist, tmp_path))


def test_gap_broken_inserts_nan_across_long_gaps() -> None:
    # A 60-day hole between the 2nd and 3rd points must break the line there,
    # while keeping every real point.
    days = [date(2026, 1, 1), date(2026, 1, 8), date(2026, 3, 9), date(2026, 3, 12)]
    xs, ys = charts._gap_broken(days, [1.0, 2.0, 3.0, 4.0], max_gap_days=30)
    assert xs == [days[0], days[1], days[2], days[2], days[3]]
    assert ys[:2] == [1.0, 2.0] and math.isnan(ys[2]) and ys[3:] == [3.0, 4.0]


def test_sport_hours_chart_writes_png(tmp_path: Path) -> None:
    from runlog.analyze.metrics import WeeklySportHours

    weekly = [
        WeeklySportHours(date(2026, 6, 1), {"Run": 3.5, "Strength": 1.0}),
        WeeklySportHours(date(2026, 6, 8), {}),
        WeeklySportHours(date(2026, 6, 15), {"Run": 2.0}),
    ]
    assert _wrote_png(charts.sport_hours_chart(weekly, tmp_path))


def test_weekday_profile_chart_writes_png(tmp_path: Path) -> None:
    from runlog.analyze.lifestyle import WeekdayProfile

    profile = WeekdayProfile(
        steps=(9000.0, 7000.0, None, 8000.0, 7500.0, 11000.0, 10000.0),
        sleep_hours=(7.0, 6.5, 7.2, None, 6.8, 8.1, 8.4),
    )
    assert _wrote_png(charts.weekday_profile_chart(profile, tmp_path))


def test_records_chart_writes_png(tmp_path: Path) -> None:
    from runlog.analyze.records import RecordEvent

    events = [
        RecordEvent(date(2025, 8, 1), "5k", "all_time", 1500.0, "5k 25:00"),
        RecordEvent(date(2026, 6, 1), "5k", "all_time", 1400.0, "5k 23:20"),
        RecordEvent(date(2026, 3, 1), "1k", "year", 230.0, "1k 3:50"),
        RecordEvent(
            date(2025, 7, 27), "biggest_week", "all_time", 90.0, "Biggest week 90.0 km"
        ),
    ]
    assert _wrote_png(charts.records_chart(events, tmp_path))
    assert _wrote_png(charts.records_chart([], tmp_path))  # empty is safe


def test_load_response_chart_writes_png(tmp_path: Path) -> None:
    from runlog.analyze.anomaly import Direction
    from runlog.analyze.response import BucketStat, MarkerResponse

    responses = [
        MarkerResponse(
            metric="hrv_sdnn",
            direction=Direction.LOW,
            buckets=(
                BucketStat("rest", 0.1, 40),
                BucketStat("moderate", -0.2, 30),
                BucketStat("hard", None, 0),  # empty bucket must not break
            ),
            pearson_r=-0.21,
            n_pairs=70,
        ),
        MarkerResponse(
            metric="sleep_hours",
            direction=Direction.LOW,
            buckets=(
                BucketStat("rest", 0.0, 40),
                BucketStat("moderate", -0.1, 30),
                BucketStat("hard", -0.5, 10),
            ),
            pearson_r=-0.3,
            n_pairs=80,
        ),
    ]
    assert _wrote_png(charts.load_response_chart(responses, tmp_path))


def test_critical_speed_and_readiness_charts_write_png(tmp_path: Path) -> None:
    from runlog.analyze.cs import CsModel, CsPoint
    from runlog.analyze.readiness import ReadinessDay

    model = CsModel(
        cs_mps=4.5,
        d_prime_m=180.0,
        r=0.99,
        points=[CsPoint(1000.0, 200.0), CsPoint(5000.0, 1100.0)],
    )
    assert _wrote_png(charts.critical_speed_chart(model, tmp_path))
    days = [ReadinessDay(date(2026, 6, d), 50.0 + d, {}) for d in range(1, 6)]
    assert _wrote_png(charts.readiness_chart(days, tmp_path))


def test_charts_handle_empty_data(tmp_path: Path) -> None:
    assert _wrote_png(charts.pace_over_time_chart([], tmp_path))
    assert _wrote_png(
        charts.training_heatmap_chart(Heatmap([], [[] for _ in range(7)]), tmp_path)
    )
    assert _wrote_png(charts.distance_histogram([], tmp_path))
    assert _wrote_png(
        charts.anomaly_timeline_chart(AnomalyReport([], [], []), tmp_path)
    )
    assert _wrote_png(charts.critical_speed_chart(None, tmp_path))
    assert _wrote_png(charts.readiness_chart([], tmp_path))
