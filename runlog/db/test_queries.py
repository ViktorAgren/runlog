"""Unit tests for the database read queries."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from runlog.db import queries, store
from runlog.domain import Activity, ActivityId, ActivityRecord, SourceId, StreamPoint


@pytest.mark.parametrize(
    ("count", "expected"),
    [(0, ""), (1, "?"), (3, "?,?,?")],
)
def test_in_placeholders(count: int, expected: str) -> None:
    assert queries.in_placeholders(count) == expected


def _store_run(
    hrs: list[float | None],
) -> tuple[sqlite3.Connection, ActivityId]:
    conn = store.connect(":memory:")  # type: ignore[arg-type]
    store.init_db(conn)
    stream = tuple(
        StreamPoint(offset_s=i, distance_m=float(i) * 3.0, hr=hr, velocity_mps=3.0)
        for i, hr in enumerate(hrs)
    )
    store.store_record(
        conn,
        ActivityRecord(
            activity=Activity(
                source="strava",
                source_id=SourceId("s:1"),
                sport_type="Run",
                start_time_utc=datetime(2026, 6, 1, 7, tzinfo=UTC),
                distance_m=float(len(hrs)) * 3.0,
                moving_s=len(hrs),
            ),
            stream=stream,
        ),
    )
    row = conn.execute("SELECT id FROM activities").fetchone()
    return conn, ActivityId(int(row[0]))


def test_hr_samples_returns_non_null_hr_for_given_runs() -> None:
    conn, activity_id = _store_run([150.0, None, 165.0])
    assert queries.hr_samples(conn, [activity_id]) == [150.0, 165.0]


def test_hr_samples_empty_for_no_runs() -> None:
    conn = store.connect(":memory:")  # type: ignore[arg-type]
    store.init_db(conn)
    assert queries.hr_samples(conn, []) == []


def _memory_db() -> sqlite3.Connection:
    conn = store.connect(":memory:")  # type: ignore[arg-type]
    store.init_db(conn)
    return conn


def test_series_store_roundtrip_is_latest_wins() -> None:
    conn = _memory_db()
    queries.replace_series(
        conn,
        "ctl",
        "training",
        "load",
        [("2026-01-01", 10.0), ("2026-01-02", 12.0)],
        "2026-01-03T00:00:00+00:00",
    )
    assert queries.read_series(conn, "ctl") == (
        "training",
        "load",
        [("2026-01-01", 10.0), ("2026-01-02", 12.0)],
    )
    queries.replace_series(
        conn, "ctl", "training", "load", [("2026-01-05", 20.0)], "later"
    )
    assert queries.read_series(conn, "ctl") == (
        "training",
        "load",
        [("2026-01-05", 20.0)],
    )
    assert queries.read_series(conn, "missing") is None


def test_all_series_rows_sorted() -> None:
    conn = _memory_db()
    queries.replace_series(conn, "b", "recovery", "ms", [("2026-01-02", 2.0)], "t")
    queries.replace_series(conn, "a", "training", "km", [("2026-01-01", 1.0)], "t")
    assert queries.all_series_rows(conn) == [
        ("a", "training", "km", "2026-01-01", 1.0),
        ("b", "recovery", "ms", "2026-01-02", 2.0),
    ]


def test_scalar_store_is_upsert() -> None:
    conn = _memory_db()
    queries.upsert_scalar(conn, "vo2max", 61.6, "ml/kg/min", "t")
    queries.upsert_scalar(conn, "vo2max", 62.0, "ml/kg/min", "later")
    assert queries.all_scalars(conn) == [("vo2max", 62.0, "ml/kg/min")]
