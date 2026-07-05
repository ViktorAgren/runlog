"""Unit tests for the SQLite storage layer."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from runlog.db import store
from runlog.domain import (
    Activity,
    ActivityRecord,
    HealthMetric,
    Lap,
    SourceId,
    StreamPoint,
)

_EXPECTED_TABLES = {
    "activities",
    "laps",
    "stream_points",
    "health_metrics",
    "activity_links",
    "raw_files",
}


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = store.connect(":memory:")  # type: ignore[arg-type]
    store.init_db(connection)
    return connection


def _sample_record(source_id: str = "42") -> ActivityRecord:
    return ActivityRecord(
        activity=Activity(
            source="strava",
            source_id=SourceId(source_id),
            sport_type="Run",
            start_time_utc=datetime(2026, 6, 1, 7, 30, tzinfo=UTC),
            distance_m=10000.0,
            avg_hr=150.0,
        ),
        laps=(Lap(lap_index=0, elapsed_s=300, distance_m=1000.0),),
        stream=(
            StreamPoint(offset_s=0, hr=120.0),
            StreamPoint(offset_s=1, hr=121.0),
        ),
    )


def test_init_db_creates_all_tables(conn: sqlite3.Connection) -> None:
    names = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert names >= _EXPECTED_TABLES


def test_store_record_is_idempotent(conn: sqlite3.Connection) -> None:
    first_id = store.store_record(conn, _sample_record())
    second_id = store.store_record(conn, _sample_record())

    assert (first_id, store.table_counts(conn)) == (
        second_id,
        {
            "activities": 1,
            "laps": 1,
            "stream_points": 2,
            "health_metrics": 0,
            "activity_links": 0,
            "raw_files": 0,
            "activities:strava": 1,
        },
    )


def test_store_record_replaces_child_rows_on_refetch(
    conn: sqlite3.Connection,
) -> None:
    store.store_record(conn, _sample_record())
    trimmed = ActivityRecord(
        activity=_sample_record().activity,
        laps=(),
        stream=(StreamPoint(offset_s=0, hr=99.0),),
    )
    store.store_record(conn, trimmed)

    remaining = conn.execute(
        "SELECT offset_s, hr FROM stream_points ORDER BY seq"
    ).fetchall()
    assert [tuple(r) for r in remaining] == [(0, 99.0)]


def test_store_record_accepts_duplicate_offsets(conn: sqlite3.Connection) -> None:
    # Real GPS tracks put several points in the same whole second; seq (position)
    # is the identity, so duplicate offset_s values must coexist.
    record = ActivityRecord(
        activity=_sample_record().activity,
        stream=(
            StreamPoint(offset_s=0, hr=120.0),
            StreamPoint(offset_s=0, hr=121.0),
            StreamPoint(offset_s=1, hr=122.0),
        ),
    )
    store.store_record(conn, record)

    rows = conn.execute(
        "SELECT seq, offset_s, hr FROM stream_points ORDER BY seq"
    ).fetchall()
    assert [tuple(r) for r in rows] == [(0, 0, 120.0), (1, 0, 121.0), (2, 1, 122.0)]


def test_insert_health_metrics_replaces_on_duplicate_key(
    conn: sqlite3.Connection,
) -> None:
    start = datetime(2026, 6, 1, tzinfo=UTC)
    store.insert_health_metrics(
        conn, [HealthMetric("resting_hr", start, 52.0, unit="count/min")]
    )
    store.insert_health_metrics(
        conn, [HealthMetric("resting_hr", start, 50.0, unit="count/min")]
    )

    rows = conn.execute("SELECT metric_type, value FROM health_metrics").fetchall()
    assert [tuple(r) for r in rows] == [("resting_hr", 50.0)]
