"""SQLite connection management and idempotent write helpers.

Re-ingesting the same activity must never duplicate rows. Activities are keyed
on ``UNIQUE(source, source_id)``; their laps and stream points are replaced
wholesale on each store so a re-fetch simply overwrites.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

from runlog.domain import ActivityId

if TYPE_CHECKING:
    from collections.abc import Iterable

    from runlog.domain import (
        Activity,
        ActivityRecord,
        HealthMetric,
        RawFile,
    )


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection with foreign keys enforced and row access by name."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create all tables if they do not already exist."""
    schema = resources.files("runlog.db").joinpath("schema.sql").read_text()
    conn.executescript(schema)
    conn.commit()


def _iso(dt: datetime) -> str:
    """Render a datetime as an ISO-8601 UTC string."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat()


def _upsert_activity(conn: sqlite3.Connection, activity: Activity) -> ActivityId:
    """Insert or update an activity, returning its internal id."""
    cur = conn.execute(
        """
        INSERT INTO activities (
            source, source_id, sport_type, start_time_utc, tz,
            elapsed_s, moving_s, distance_m, avg_hr, max_hr,
            avg_pace_s_per_km, avg_cadence, elevation_gain_m, calories,
            name, raw_path
        )
        VALUES (
            :source, :source_id, :sport_type, :start_time_utc, :tz,
            :elapsed_s, :moving_s, :distance_m, :avg_hr, :max_hr,
            :avg_pace_s_per_km, :avg_cadence, :elevation_gain_m, :calories,
            :name, :raw_path
        )
        ON CONFLICT (source, source_id) DO UPDATE SET
            sport_type = excluded.sport_type,
            start_time_utc = excluded.start_time_utc,
            tz = excluded.tz,
            elapsed_s = excluded.elapsed_s,
            moving_s = excluded.moving_s,
            distance_m = excluded.distance_m,
            avg_hr = excluded.avg_hr,
            max_hr = excluded.max_hr,
            avg_pace_s_per_km = excluded.avg_pace_s_per_km,
            avg_cadence = excluded.avg_cadence,
            elevation_gain_m = excluded.elevation_gain_m,
            calories = excluded.calories,
            name = excluded.name,
            raw_path = excluded.raw_path
        RETURNING id
        """,
        {
            "source": activity.source,
            "source_id": str(activity.source_id),
            "sport_type": activity.sport_type,
            "start_time_utc": _iso(activity.start_time_utc),
            "tz": activity.tz,
            "elapsed_s": activity.elapsed_s,
            "moving_s": activity.moving_s,
            "distance_m": activity.distance_m,
            "avg_hr": activity.avg_hr,
            "max_hr": activity.max_hr,
            "avg_pace_s_per_km": activity.avg_pace_s_per_km,
            "avg_cadence": activity.avg_cadence,
            "elevation_gain_m": activity.elevation_gain_m,
            "calories": activity.calories,
            "name": activity.name,
            "raw_path": activity.raw_path,
        },
    )
    row = cur.fetchone()
    return ActivityId(int(row["id"]))


def store_record(conn: sqlite3.Connection, record: ActivityRecord) -> ActivityId:
    """Persist an activity with its laps and stream, replacing prior child rows.

    Commits on success. Returns the internal activity id.
    """
    activity_id = _upsert_activity(conn, record.activity)
    conn.execute("DELETE FROM laps WHERE activity_id = ?", (activity_id,))
    conn.execute("DELETE FROM stream_points WHERE activity_id = ?", (activity_id,))
    conn.executemany(
        """
        INSERT INTO laps (
            activity_id, lap_index, elapsed_s, distance_m,
            avg_hr, avg_pace_s_per_km
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                activity_id,
                lap.lap_index,
                lap.elapsed_s,
                lap.distance_m,
                lap.avg_hr,
                lap.avg_pace_s_per_km,
            )
            for lap in record.laps
        ],
    )
    conn.executemany(
        """
        INSERT INTO stream_points (
            activity_id, seq, offset_s, distance_m, lat, lng,
            altitude_m, hr, cadence, velocity_mps, watts
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                activity_id,
                seq,
                point.offset_s,
                point.distance_m,
                point.lat,
                point.lng,
                point.altitude_m,
                point.hr,
                point.cadence,
                point.velocity_mps,
                point.watts,
            )
            for seq, point in enumerate(record.stream)
        ],
    )
    conn.commit()
    return activity_id


def insert_health_metrics(
    conn: sqlite3.Connection, metrics: Iterable[HealthMetric]
) -> int:
    """Insert health metrics, replacing on (metric_type, start_time). Commits.

    Returns the number of rows written.
    """
    rows = [
        (
            m.metric_type,
            _iso(m.start_time_utc),
            _iso(m.end_time_utc) if m.end_time_utc else None,
            m.value,
            m.unit,
            m.source,
        )
        for m in metrics
    ]
    conn.executemany(
        """
        INSERT OR REPLACE INTO health_metrics (
            metric_type, start_time_utc, end_time_utc, value, unit, source
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def record_raw_file(conn: sqlite3.Connection, raw: RawFile) -> None:
    """Record a verbatim raw payload in the manifest. Commits."""
    conn.execute(
        """
        INSERT OR REPLACE INTO raw_files (path, source, source_id, fetched_at, sha256)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            raw.path,
            raw.source,
            str(raw.source_id),
            _iso(raw.fetched_at),
            raw.sha256,
        ),
    )
    conn.commit()


def insert_link(
    conn: sqlite3.Connection,
    strava_activity_id: ActivityId,
    apple_activity_id: ActivityId,
    match_confidence: float,
) -> None:
    """Record a Strava<->Apple match. Commits."""
    conn.execute(
        """
        INSERT OR REPLACE INTO activity_links (
            strava_activity_id, apple_activity_id, match_confidence
        )
        VALUES (?, ?, ?)
        """,
        (strava_activity_id, apple_activity_id, match_confidence),
    )
    conn.commit()


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Return row counts per table, plus per-source activity counts."""
    tables = (
        "activities",
        "laps",
        "stream_points",
        "health_metrics",
        "activity_links",
        "raw_files",
    )
    counts = {
        table: int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
        for table in tables
    }
    for row in conn.execute(
        "SELECT source, COUNT(*) AS n FROM activities GROUP BY source"
    ):
        counts[f"activities:{row['source']}"] = int(row["n"])
    return counts


def latest_start(conn: sqlite3.Connection, source: str) -> datetime | None:
    """Return the most recent activity start time for a source, if any.

    Used to bound incremental Strava syncs to activities newer than what we
    already hold.
    """
    row = conn.execute(
        "SELECT MAX(start_time_utc) AS latest FROM activities WHERE source = ?",
        (source,),
    ).fetchone()
    if row is None or row["latest"] is None:
        return None
    return datetime.fromisoformat(row["latest"])
