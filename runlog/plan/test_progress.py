"""Unit tests for the plan-progress builder."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime

import pytest

from runlog.analyze import metrics
from runlog.db import store
from runlog.domain import Activity, ActivityRecord, Lap, SourceId, StreamPoint
from runlog.plan.progress import build_progress, workout_detail


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = store.connect(":memory:")  # type: ignore[arg-type]
    store.init_db(connection)
    return connection


def _add_run(
    conn: sqlite3.Connection, when: datetime, *, distance_m: float = 5000.0
) -> None:
    stream = tuple(
        StreamPoint(offset_s=i, distance_m=float(i) * 3.0, hr=150.0)
        for i in range(0, 400)
    )
    store.store_record(
        conn,
        ActivityRecord(
            activity=Activity(
                source="strava",
                source_id=SourceId(f"s:{when.isoformat()}"),
                sport_type="Run",
                start_time_utc=when,
                distance_m=distance_m,
                moving_s=1500,
                avg_pace_s_per_km=300.0,
                avg_hr=150.0,
            ),
            stream=stream,
        ),
    )


def _add_structured_run(conn: sqlite3.Connection, when: datetime) -> None:
    # WU (slow) then three even reps (fast), with a rising HR stream so per-lap
    # max HR is recoverable and rep HR drifts up across the set.
    laps = (
        Lap(
            lap_index=0,
            elapsed_s=100,
            distance_m=250.0,
            avg_pace_s_per_km=400.0,
            avg_hr=140.0,
        ),
        Lap(
            lap_index=1,
            elapsed_s=60,
            distance_m=250.0,
            avg_pace_s_per_km=240.0,
            avg_hr=160.0,
        ),
        Lap(
            lap_index=2,
            elapsed_s=60,
            distance_m=250.0,
            avg_pace_s_per_km=240.0,
            avg_hr=170.0,
        ),
        Lap(
            lap_index=3,
            elapsed_s=60,
            distance_m=250.0,
            avg_pace_s_per_km=240.0,
            avg_hr=178.0,
        ),
    )
    stream = tuple(
        StreamPoint(offset_s=i, distance_m=float(i) * 3.6, hr=float(150 + i % 40))
        for i in range(0, 281)
    )
    store.store_record(
        conn,
        ActivityRecord(
            activity=Activity(
                source="strava",
                source_id=SourceId(f"s:struct:{when.isoformat()}"),
                sport_type="Run",
                start_time_utc=when,
                distance_m=1000.0,
                moving_s=280,
                avg_pace_s_per_km=280.0,
                avg_hr=165.0,
            ),
            laps=laps,
            stream=stream,
        ),
    )


def test_workout_detail_surfaces_per_rep_max_hr_and_rep_set_stats(
    conn: sqlite3.Connection,
) -> None:
    _add_structured_run(conn, datetime(2026, 7, 6, 7, tzinfo=UTC))
    run = metrics.canonical_run_activities(conn, min_distance_km=0.0)[0]

    detail = workout_detail(conn, run, hr_max=190.0)

    # Every lap gets a per-rep max HR from the stream, and the three even reps
    # (avg HR 160 -> 178) give a drift of +18 with zero pace variation.
    assert all(lap.max_hr is not None for lap in detail.laps)
    assert (detail.rep_hr_drift, detail.rep_pace_cv) == (18.0, 0.0)


def test_workout_detail_surfaces_dynamics_drift_and_economy(
    conn: sqlite3.Connection,
) -> None:
    # HR drifts up while speed stays flat -> positive cardiac drift; power and
    # cadence flow from the activity; economy = speed / power.
    stream = tuple(
        StreamPoint(
            offset_s=i, distance_m=float(i) * 3.0, hr=150.0 + i * 0.05, velocity_mps=3.0
        )
        for i in range(0, 400)
    )
    store.store_record(
        conn,
        ActivityRecord(
            activity=Activity(
                source="strava",
                source_id=SourceId("s:dyn"),
                sport_type="Run",
                start_time_utc=datetime(2026, 7, 6, 7, tzinfo=UTC),
                distance_m=5000.0,
                moving_s=1500,
                avg_pace_s_per_km=300.0,
                avg_hr=155.0,
                avg_cadence=170.0,
                avg_power_w=250.0,
                avg_stride_length_m=1.2,
            ),
            stream=stream,
        ),
    )
    run = metrics.canonical_run_activities(conn, min_distance_km=0.0)[0]

    detail = workout_detail(conn, run, hr_max=190.0)

    assert (detail.avg_cadence, detail.avg_power_w, detail.avg_stride_length_m) == (
        170.0,
        250.0,
        1.2,
    )
    # Economy = (5000/1500 m/s) / 250 W = 0.0133; drift positive (efficiency fell).
    assert detail.running_economy == 0.0133
    assert detail.cardiac_drift_pct is not None and detail.cardiac_drift_pct > 0


def test_workout_detail_dynamics_none_without_data(conn: sqlite3.Connection) -> None:
    _add_run(conn, datetime(2026, 7, 6, 7, tzinfo=UTC))  # no power/cadence set
    run = metrics.canonical_run_activities(conn, min_distance_km=0.0)[0]

    detail = workout_detail(conn, run, hr_max=190.0)
    assert (detail.avg_power_w, detail.running_economy) == (None, None)


def test_build_progress_counts_only_runs_since_start(conn: sqlite3.Connection) -> None:
    # One run before the block, two inside it: only the latter two count.
    _add_run(conn, datetime(2026, 6, 20, 7, tzinfo=UTC), distance_m=8000.0)  # before
    _add_run(conn, datetime(2026, 7, 6, 7, tzinfo=UTC), distance_m=5000.0)
    _add_run(conn, datetime(2026, 7, 8, 7, tzinfo=UTC), distance_m=6000.0)

    progress = build_progress(
        conn, start=date(2026, 7, 5), hr_max=195.0, today=date(2026, 7, 12)
    )

    assert (progress.total_runs, progress.total_km, progress.weeks_elapsed) == (
        2,
        11.0,
        1,
    )
    assert progress.longest_run_km == 6.0
    assert progress.ctl_now is not None  # PMC computed over full history
    assert "1k" in progress.best_efforts  # from the in-window streams

    # Per-workout detail: one row per in-window run, with an HR-zone split.
    assert [w.day for w in progress.workouts] == [date(2026, 7, 6), date(2026, 7, 8)]
    assert progress.workouts[0].moderate_pct == 100.0  # 150 bpm at HRmax 195 = Z3
    assert progress.workouts[0].kind == "Moderate"
    # Both runs fall in plan week 1 (start .. start+6).
    assert [(pw.week, pw.runs) for pw in progress.plan_weeks] == [(1, 2)]
