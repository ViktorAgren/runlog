"""Unit tests for the high-level analytics calculations."""

from __future__ import annotations

import math
import sqlite3
from datetime import UTC, date, datetime, timedelta

from runlog.analyze import analytics, metrics
from runlog.analyze.metrics import Run
from runlog.db import store
from runlog.domain import Activity, ActivityId, ActivityRecord, SourceId, StreamPoint


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


def test_best_effort_seconds_ignores_mid_stream_teleport() -> None:
    # 300 s at 3 m/s (real), then a GPS teleport dumps 200 m in 1 s, then more
    # real running. The fastest 200 m must be the clean ~66.7 s, never the
    # teleport-inflated window that would read far too fast.
    real = [(t, float(t) * 3) for t in range(0, 301)]
    teleport = [(301, real[-1][1] + 200.0)]  # +200 m in 1 s
    after = [(301 + t, teleport[0][1] + t * 3) for t in range(1, 60)]
    seconds = analytics.best_effort_seconds([*real, *teleport, *after], 200.0)
    assert seconds is not None
    # 200 m at 3 m/s = 66.67 s; the sampled window lands at 67 s, never <30.
    assert seconds >= 60.0


def _add_run_with_stream(
    conn: sqlite3.Connection, when: datetime, speed_mps: float, key: str
) -> None:
    seconds = int(2000 / speed_mps)
    stream = tuple(
        StreamPoint(offset_s=i, distance_m=speed_mps * i, velocity_mps=speed_mps)
        for i in range(seconds + 1)
    )
    store.store_record(
        conn,
        ActivityRecord(
            activity=Activity(
                source="strava",
                source_id=SourceId(key),
                sport_type="Run",
                start_time_utc=when,
                distance_m=2000.0,
                moving_s=seconds,
                avg_pace_s_per_km=1000 / speed_mps,
            ),
            stream=stream,
        ),
    )


def test_run_effort_series_per_run_values() -> None:
    # Two runs: the second is slower. Unlike best_effort_progressions (monotone),
    # run_effort_series must report the slower value for the second run.
    conn = store.connect(":memory:")  # type: ignore[arg-type]
    store.init_db(conn)
    _add_run_with_stream(conn, datetime(2026, 6, 1, 7, tzinfo=UTC), 5.0, "a")  # 200s/1k
    _add_run_with_stream(conn, datetime(2026, 6, 8, 7, tzinfo=UTC), 4.0, "b")  # 250s/1k
    runs = metrics.canonical_run_activities(conn)

    series = analytics.run_effort_series(conn, runs, 1000.0)
    assert series == [(date(2026, 6, 1), 200.0), (date(2026, 6, 8), 250.0)]


def test_best_effort_records_uses_continuous_segments() -> None:
    # A single 2 km run at a constant 5 m/s: 1k in 200 s and 2k in 400 s, both
    # at 200 s/km (3:20/km) — continuous efforts pulled from the stream, dated.
    conn = store.connect(":memory:")  # type: ignore[arg-type]
    store.init_db(conn)
    _add_run_with_stream(conn, datetime(2026, 6, 1, 7, tzinfo=UTC), 5.0, "fast")
    runs = metrics.canonical_run_activities(conn)

    records = {
        e.label: (e.seconds, e.pace_s_per_km, e.when)
        for e in analytics.best_effort_records(
            conn, runs, [("1k", 1000.0), ("2k", 2000.0)]
        )
    }
    assert records["1k"] == (200.0, 200.0, date(2026, 6, 1))
    assert records["2k"] == (400.0, 200.0, date(2026, 6, 1))


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
