"""Unit tests for the personal-record timeline."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime

import pytest

from runlog.analyze import metrics, records
from runlog.db import store
from runlog.domain import Activity, ActivityRecord, SourceId, StreamPoint


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = store.connect(":memory:")  # type: ignore[arg-type]
    store.init_db(connection)
    return connection


def _add_run(
    conn: sqlite3.Connection,
    when: datetime,
    *,
    distance_m: float,
    speed_mps: float,
    with_stream: bool = True,
) -> None:
    seconds = int(distance_m / speed_mps)
    stream = (
        tuple(
            StreamPoint(offset_s=i, distance_m=speed_mps * i, velocity_mps=speed_mps)
            for i in range(seconds + 1)
        )
        if with_stream
        else ()
    )
    store.store_record(
        conn,
        ActivityRecord(
            activity=Activity(
                source="strava",
                source_id=SourceId(when.isoformat()),
                sport_type="Run",
                start_time_utc=when,
                distance_m=distance_m,
                moving_s=seconds,
                avg_pace_s_per_km=1000 / speed_mps,
            ),
            stream=stream,
        ),
    )


def _timeline(conn: sqlite3.Connection) -> list[records.RecordEvent]:
    return records.records_timeline(conn, metrics.canonical_run_activities(conn))


def test_all_time_effort_events_in_order(conn: sqlite3.Connection) -> None:
    # First 5 km at 4 m/s (1250 s), then a faster 5 km at 5 m/s (1000 s).
    _add_run(
        conn, datetime(2026, 6, 1, 7, tzinfo=UTC), distance_m=5000.0, speed_mps=4.0
    )
    _add_run(
        conn, datetime(2026, 6, 8, 7, tzinfo=UTC), distance_m=5000.0, speed_mps=5.0
    )

    five_k = [e for e in _timeline(conn) if e.kind == "5k"]
    assert [(e.day, e.scope, e.value) for e in five_k] == [
        (date(2026, 6, 1), "all_time", 1250.0),
        (date(2026, 6, 8), "all_time", 1000.0),
    ]


def test_year_event_when_beaten_by_earlier_year_all_time(
    conn: sqlite3.Connection,
) -> None:
    # 2025 sets a fast 1k (250 s, all-time). 2026's best 1k (300 s) can't beat
    # it -> a *year* record for 2026, not an all-time one.
    _add_run(
        conn, datetime(2025, 6, 1, 7, tzinfo=UTC), distance_m=1000.0, speed_mps=4.0
    )
    _add_run(
        conn, datetime(2026, 6, 1, 7, tzinfo=UTC), distance_m=1000.0, speed_mps=10 / 3
    )

    one_k = [e for e in _timeline(conn) if e.kind == "1k"]
    assert [(e.day.year, e.scope) for e in one_k] == [
        (2025, "all_time"),
        (2026, "year"),
    ]


def test_no_year_event_on_all_time_pr_day(conn: sqlite3.Connection) -> None:
    # A single run can't produce both an all-time and a year event for one kind.
    _add_run(
        conn, datetime(2026, 6, 1, 7, tzinfo=UTC), distance_m=1000.0, speed_mps=4.0
    )
    one_k = [e for e in _timeline(conn) if e.kind == "1k"]
    assert [e.scope for e in one_k] == ["all_time"]


def test_biggest_week_event(conn: sqlite3.Connection) -> None:
    # Two runs the same ISO week -> one biggest-week record dated to week end.
    _add_run(
        conn, datetime(2026, 6, 1, 7, tzinfo=UTC), distance_m=5000.0, speed_mps=4.0
    )
    _add_run(
        conn, datetime(2026, 6, 3, 7, tzinfo=UTC), distance_m=6000.0, speed_mps=4.0
    )

    weeks = [e for e in _timeline(conn) if e.kind == "biggest_week"]
    assert [(e.day, e.scope, e.value) for e in weeks] == [
        (date(2026, 6, 7), "all_time", 11.0)  # week Mon Jun 1 .. Sun Jun 7
    ]


def test_new_records_window(conn: sqlite3.Connection) -> None:
    _add_run(
        conn, datetime(2026, 6, 1, 7, tzinfo=UTC), distance_m=5000.0, speed_mps=4.0
    )
    _add_run(
        conn, datetime(2026, 6, 20, 7, tzinfo=UTC), distance_m=5000.0, speed_mps=5.0
    )
    fresh = records.new_records(_timeline(conn), since=date(2026, 6, 15))
    assert all(e.day >= date(2026, 6, 15) for e in fresh)
    assert any(e.kind == "5k" and e.day == date(2026, 6, 20) for e in fresh)


def test_runs_without_streams_yield_distance_and_week_only(
    conn: sqlite3.Connection,
) -> None:
    _add_run(
        conn,
        datetime(2026, 6, 1, 7, tzinfo=UTC),
        distance_m=8000.0,
        speed_mps=4.0,
        with_stream=False,
    )
    kinds = {e.kind for e in _timeline(conn)}
    assert kinds == {"longest_run", "biggest_week"}
