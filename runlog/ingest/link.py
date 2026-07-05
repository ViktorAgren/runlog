"""Match the same run across Strava and Apple Health by start-time proximity.

The same outdoor run is often recorded by both sources with slightly different
start times. Rather than merge (and risk dropping data), we record a link in
``activity_links`` with a confidence score. Matching is greedy: closest pairs
first, each activity used at most once.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from runlog.db import store
from runlog.domain import ActivityId

if TYPE_CHECKING:
    import sqlite3

_DEFAULT_WINDOW_S = 180


def _activity_starts(
    conn: sqlite3.Connection, source: str
) -> list[tuple[ActivityId, datetime]]:
    return [
        (ActivityId(int(row["id"])), datetime.fromisoformat(row["start_time_utc"]))
        for row in conn.execute(
            "SELECT id, start_time_utc FROM activities WHERE source = ?",
            (source,),
        )
    ]


def link_activities(conn: sqlite3.Connection, window_s: int = _DEFAULT_WINDOW_S) -> int:
    """Link Strava/Apple activities starting within ``window_s``. Returns links."""
    strava = _activity_starts(conn, "strava")
    apple = _activity_starts(conn, "apple_health")

    candidates = sorted(
        (abs((s_start - a_start).total_seconds()), s_id, a_id)
        for s_id, s_start in strava
        for a_id, a_start in apple
        if abs((s_start - a_start).total_seconds()) <= window_s
    )

    used_strava: set[ActivityId] = set()
    used_apple: set[ActivityId] = set()
    linked = 0
    for delta, s_id, a_id in candidates:
        if s_id in used_strava or a_id in used_apple:
            continue
        store.insert_link(conn, s_id, a_id, round(1 - delta / window_s, 3))
        used_strava.add(s_id)
        used_apple.add(a_id)
        linked += 1
    return linked
