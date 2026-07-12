"""Unit tests for the high-level analytics calculations."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

from runlog.analyze import analytics
from runlog.analyze.metrics import Run
from runlog.domain import ActivityId


def _run(when: datetime, *, avg_hr: float | None = 150.0, moving_s: int = 1800) -> Run:
    return Run(
        activity_id=ActivityId(1),
        source="strava",
        start=when,
        distance_m=5000.0,
        moving_s=moving_s,
        avg_pace_s_per_km=moving_s / 5,
        avg_hr=avg_hr,
        max_hr=None,
    )


def test_linear_trend_recovers_slope() -> None:
    points = [(date(2026, 1, 1), 10.0), (date(2026, 1, 11), 20.0)]
    trend = analytics.linear_trend(points)
    assert trend is not None
    assert (round(trend.slope_per_day, 3), round(trend.r, 3)) == (1.0, 1.0)


def test_run_trimp_matches_banister_formula() -> None:
    run = _run(datetime(2026, 1, 1), avg_hr=150.0, moving_s=3600)
    # HRr = (150-50)/(190-50) = 0.714..., TRIMP = 60 * HRr * 0.64 * e^(1.92*HRr)
    hrr = (150 - 50) / (190 - 50)
    expected = 60 * hrr * 0.64 * math.exp(1.92 * hrr)
    got = analytics.run_trimp(run, hr_max=190.0, hr_rest=50.0)
    assert got is not None and math.isclose(got, expected, rel_tol=1e-9)


def test_performance_management_ewma_and_form() -> None:
    daily = [(date(2026, 1, 1), 100.0)]
    pmc = analytics.performance_management(daily)
    # First day: form uses pre-load balance (0), then CTL/ATL take one EWMA step.
    first = pmc[0]
    assert (round(first.fitness, 4), round(first.fatigue, 4), first.form) == (
        round(100 / 42, 4),
        round(100 / 7, 4),
        0.0,
    )


def test_acwr_ratio_after_warmup() -> None:
    # 28 days at 50 TRIMP/day -> acute mean == chronic mean -> ratio 1.0.
    daily = [(date(2026, 1, 1) + timedelta(days=i), 50.0) for i in range(28)]
    series = analytics.acwr_series(daily)
    assert series[-1][1] == 1.0


def test_best_effort_seconds_sliding_window() -> None:
    # 1 m/s for 100 s covering 100 m: fastest 50 m takes 50 s.
    points = [(t, float(t)) for t in range(0, 101)]
    assert analytics.best_effort_seconds(points, 50.0) == 50.0


def test_best_effort_seconds_rejects_stream_glitch() -> None:
    # Corrupted stream: 1000 m appears in 1 s (impossibly fast) -> rejected;
    # a real 1000 m at 4 m/s (250 s) is accepted.
    glitch = [(0, 0.0), (1, 1000.0)]
    real = [(t, float(t) * 4) for t in range(0, 300)]
    assert (
        analytics.best_effort_seconds(glitch, 1000.0),
        analytics.best_effort_seconds(real, 1000.0),
    ) == (None, 250.0)


def test_fill_daily_zero_fills_rest_days() -> None:
    daily = [(date(2026, 1, 1), 10.0), (date(2026, 1, 3), 30.0)]
    assert analytics.fill_daily(daily) == [
        (date(2026, 1, 1), 10.0),
        (date(2026, 1, 2), 0.0),
        (date(2026, 1, 3), 30.0),
    ]


def test_pearson_perfect_and_flat() -> None:
    assert analytics.pearson([1.0, 2.0, 3.0], [2.0, 4.0, 6.0]) == 1.0
    assert analytics.pearson([1.0, 2.0, 3.0], [5.0, 5.0, 5.0]) is None


def test_efficiency_factor_speed_per_hr() -> None:
    run = _run(datetime(2026, 6, 1), avg_hr=150.0, moving_s=1500)
    # 5000 m / 25 min = 200 m/min; /150 bpm = 1.333.
    points = analytics.efficiency_factor([run])
    assert points == [(date(2026, 6, 1), 1.333)]
