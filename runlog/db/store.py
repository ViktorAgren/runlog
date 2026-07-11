"""SQLite connection management and idempotent write helpers.

Re-ingesting the same activity must never duplicate rows. Activities are keyed
on ``UNIQUE(source, source_id)``; their laps and stream points are replaced
wholesale on each store so a re-fetch simply overwrites.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

from runlog.db import quality
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


# Activity columns persisted, in order. Names match `Activity` dataclass fields
# (except source_id / start_time_utc, which are serialized in `_activity_params`).
_ACTIVITY_COLUMNS: tuple[str, ...] = (
    "source",
    "source_id",
    "sport_type",
    "start_time_utc",
    "tz",
    "elapsed_s",
    "moving_s",
    "distance_m",
    "avg_hr",
    "max_hr",
    "avg_pace_s_per_km",
    "avg_cadence",
    "elevation_gain_m",
    "calories",
    "name",
    "raw_path",
    "relative_effort",
    "grade_adj_distance_m",
    "max_speed_mps",
    "elevation_loss_m",
    "avg_grade",
    "max_grade",
    "avg_watts",
    "training_load",
    "intensity",
    "temp_c",
    "humidity",
    "wind_mps",
    "avg_power_w",
    "avg_stride_length_m",
    "avg_vertical_oscillation_cm",
    "avg_ground_contact_ms",
    "avg_running_speed_mps",
    "quality_flags",
)
_ACTIVITY_KEY = ("source", "source_id")


def _activity_params(activity: Activity) -> dict[str, object]:
    params: dict[str, object] = {c: getattr(activity, c) for c in _ACTIVITY_COLUMNS}
    params["source_id"] = str(activity.source_id)
    params["start_time_utc"] = _iso(activity.start_time_utc)
    return params


def _upsert_activity(conn: sqlite3.Connection, activity: Activity) -> ActivityId:
    """Insert or update an activity, returning its internal id."""
    columns = ", ".join(_ACTIVITY_COLUMNS)
    placeholders = ", ".join(f":{c}" for c in _ACTIVITY_COLUMNS)
    updates = ", ".join(
        f"{c} = excluded.{c}" for c in _ACTIVITY_COLUMNS if c not in _ACTIVITY_KEY
    )
    cur = conn.execute(
        f"INSERT INTO activities ({columns}) VALUES ({placeholders}) "
        f"ON CONFLICT (source, source_id) DO UPDATE SET {updates} RETURNING id",
        _activity_params(activity),
    )
    row = cur.fetchone()
    return ActivityId(int(row["id"]))


def store_record(conn: sqlite3.Connection, record: ActivityRecord) -> ActivityId:
    """Persist an activity with its laps and stream, replacing prior child rows.

    Commits on success. Returns the internal activity id.
    """
    flags = quality.stream_flags(record.activity, record.stream)
    activity = dataclasses.replace(
        record.activity, quality_flags=",".join(flags) or None
    )
    activity_id = _upsert_activity(conn, activity)
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
