"""Unit tests for the Strava<->Apple activity matcher."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from runlog.db import store
from runlog.domain import Activity, ActivityRecord, Source, SourceId
from runlog.ingest.link import link_activities


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = store.connect(":memory:")  # type: ignore[arg-type]
    store.init_db(connection)
    return connection


def _add(conn: sqlite3.Connection, source: Source, when: datetime) -> None:
    store.store_record(
        conn,
        ActivityRecord(
            activity=Activity(
                source=source,
                source_id=SourceId(f"{source}:{when.isoformat()}"),
                sport_type="Run",
                start_time_utc=when,
            )
        ),
    )


def test_link_activities_matches_closest_within_window(
    conn: sqlite3.Connection,
) -> None:
    _add(conn, "strava", datetime(2026, 6, 1, 7, 30, 0, tzinfo=UTC))
    _add(conn, "apple_health", datetime(2026, 6, 1, 7, 31, 0, tzinfo=UTC))  # +60s
    _add(conn, "strava", datetime(2026, 6, 2, 9, 0, 0, tzinfo=UTC))  # unmatched

    linked = link_activities(conn, window_s=180)

    rows = conn.execute("SELECT match_confidence FROM activity_links").fetchall()
    # 60s of a 180s window -> confidence 1 - 60/180 = 0.667.
    assert (linked, [r["match_confidence"] for r in rows]) == (1, [0.667])


def test_link_activities_ignores_pairs_outside_window(
    conn: sqlite3.Connection,
) -> None:
    _add(conn, "strava", datetime(2026, 6, 1, 7, 30, 0, tzinfo=UTC))
    _add(conn, "apple_health", datetime(2026, 6, 1, 7, 40, 0, tzinfo=UTC))  # +600s

    assert link_activities(conn, window_s=180) == 0
