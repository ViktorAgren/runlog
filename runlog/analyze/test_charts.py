"""Smoke tests for chart rendering (Agg backend, writes real PNGs)."""

from __future__ import annotations

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


def test_pace_by_weekday_chart_writes_png(tmp_path: Path) -> None:
    weekday_paces: list[list[float]] = [[300.0, 310.0], [], [290.0], [], [], [], []]
    assert _wrote_png(charts.pace_by_weekday_chart(weekday_paces, tmp_path))


def test_hr_charts_write_png(tmp_path: Path) -> None:
    hr_points = [
        HrPoint(datetime(2026, 6, d, tzinfo=UTC), 150.0, 180.0) for d in range(1, 5)
    ]
    assert _wrote_png(charts.hr_over_time_chart(hr_points, tmp_path))
    assert _wrote_png(charts.hr_histogram([120.0, 130.0, 140.0, 150.0], tmp_path))


def test_efficiency_and_marker_charts_write_png(tmp_path: Path) -> None:
    points = [PacePoint(datetime(2026, 6, 1, tzinfo=UTC), 300.0, 5.0, 150.0)]
    daily = [(date(2026, 6, d), 50.0 + d) for d in range(1, 4)]
    assert _wrote_png(charts.efficiency_chart(points, tmp_path))
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
    assert _wrote_png(charts.rest_gap_histogram([1, 2, 2, 5], tmp_path))
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
    assert _wrote_png(charts.start_hour_chart([6, 7, 7, 18], tmp_path))
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


def test_charts_handle_empty_data(tmp_path: Path) -> None:
    assert _wrote_png(charts.pace_over_time_chart([], tmp_path))
    assert _wrote_png(
        charts.training_heatmap_chart(Heatmap([], [[] for _ in range(7)]), tmp_path)
    )
    assert _wrote_png(charts.distance_histogram([], tmp_path))
    assert _wrote_png(
        charts.anomaly_timeline_chart(AnomalyReport([], [], []), tmp_path)
    )
