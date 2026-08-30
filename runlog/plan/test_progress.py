"""Unit tests for the plan-progress builder."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime

import pytest

from runlog.db import store
from runlog.domain import Activity, ActivityRecord, SourceId, StreamPoint
from runlog.plan.progress import build_progress


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
    # 150 bpm at HRmax 195 / resting 50 is (150-50)/(195-50)=69% reserve = Z2 easy
    # under Karvonen (the old %HRmax model wrongly called it Z3 moderate).
    assert progress.workouts[0].easy_pct == 100.0
    assert progress.workouts[0].kind == "Easy"
    # Both runs fall in plan week 1 (start .. start+6).
    assert [(pw.week, pw.runs) for pw in progress.plan_weeks] == [(1, 2)]
