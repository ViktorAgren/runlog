"""Unit tests for the athlete-profile builder."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from runlog.db import store
from runlog.domain import Activity, ActivityRecord, SourceId, StreamPoint
from runlog.plan.profile import build_profile


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = store.connect(":memory:")  # type: ignore[arg-type]
    store.init_db(connection)
    return connection


def _add_run(
    conn: sqlite3.Connection,
    when: datetime,
    *,
    distance_m: float = 5000.0,
    pace: float = 300.0,
    avg_hr: float = 150.0,
) -> None:
    # A cumulative distance/time stream so best-effort extraction has data.
    stream = tuple(
        StreamPoint(offset_s=i, distance_m=float(i) * 3.0, hr=avg_hr)
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
                avg_pace_s_per_km=pace,
                avg_hr=avg_hr,
            ),
            stream=stream,
        ),
    )


def test_build_profile_summarizes_history(conn: sqlite3.Connection) -> None:
    _add_run(conn, datetime(2026, 6, 1, 7, tzinfo=UTC), distance_m=5000.0, pace=300.0)
    _add_run(conn, datetime(2026, 6, 8, 7, tzinfo=UTC), distance_m=10000.0, pace=320.0)

    profile = build_profile(conn)

    assert (profile.run_count, profile.total_km, profile.longest_run_km) == (
        2,
        15.0,
        10.0,
    )
    assert profile.typical_pace_s_per_km == 310.0  # median of 300, 320
    assert profile.fitness_ctl is not None  # TRIMP-based PMC produced a value
    assert "1k" in profile.best_efforts  # extracted from the streams


def test_build_profile_includes_advanced_fitness(conn: sqlite3.Connection) -> None:
    _add_run(conn, datetime(2026, 6, 1, 7, tzinfo=UTC))
    _add_run(conn, datetime(2026, 6, 8, 7, tzinfo=UTC))

    profile = build_profile(conn)

    assert profile.advanced is not None
    # 400 m + 1 k efforts from the stream fit a critical-speed model, and best
    # efforts are surfaced for the planner to anchor zones against.
    assert profile.advanced.critical_speed is not None
    assert any(e.label == "1k" for e in profile.advanced.best_effort_records)


def test_build_profile_derives_vdot_and_zones(conn: sqlite3.Connection) -> None:
    _add_run(conn, datetime(2026, 6, 1, 7, tzinfo=UTC))
    profile = build_profile(conn, hr_max=190.0)

    assert profile.hr_max == 190.0
    assert profile.vdot is not None  # computed from a 5k effort
    kinds = [z.kind for z in profile.zones]
    assert kinds == ["Recovery", "Easy", "Marathon", "Threshold", "Interval", "Rep"]
