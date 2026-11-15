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


def _greedy_time_matches(
    left: list[tuple[ActivityId, datetime]],
    right: list[tuple[ActivityId, datetime]],
    window_s: int,
) -> list[tuple[ActivityId, ActivityId, float]]:
    """Pair each left/right activity to its nearest twin within ``window_s``.

    Greedy: closest pairs first, each activity used at most once. The returned
    confidence is 1.0 for a simultaneous start, decaying to 0 at the window edge.
    """
    candidates = sorted(
        (abs((l_start - r_start).total_seconds()), l_id, r_id)
        for l_id, l_start in left
        for r_id, r_start in right
        if abs((l_start - r_start).total_seconds()) <= window_s
    )
    used_left: set[ActivityId] = set()
    used_right: set[ActivityId] = set()
    matches: list[tuple[ActivityId, ActivityId, float]] = []
    for delta, l_id, r_id in candidates:
        if l_id in used_left or r_id in used_right:
            continue
        matches.append((l_id, r_id, round(1 - delta / window_s, 3)))
        used_left.add(l_id)
        used_right.add(r_id)
    return matches


def link_activities(conn: sqlite3.Connection, window_s: int = _DEFAULT_WINDOW_S) -> int:
    """Link Strava/Apple activities starting within ``window_s``. Returns links."""
    matches = _greedy_time_matches(
        _activity_starts(conn, "strava"),
        _activity_starts(conn, "apple_health"),
        window_s,
    )
    for strava_id, apple_id, confidence in matches:
        store.insert_link(conn, strava_id, apple_id, confidence)
    return len(matches)


def link_runna_apple(
    conn: sqlite3.Connection, window_s: int = _DEFAULT_WINDOW_S
) -> int:
    """Link each Apple run to its nearest Runna run within ``window_s``. Returns links.

    Runna embeds its Strava id (linked exactly at ingest), but a treadmill run
    recorded only by Runna and the watch has no Strava row to go through. Apple
    also double-logs a session as two overlapping workouts, so a Runna run (the
    authoritative record) absorbs every Apple twin in its window, not just one.
    """
    runna = _activity_starts(conn, "runna")
    apple = _activity_starts(conn, "apple_health")
    candidates = sorted(
        (abs((r_start - a_start).total_seconds()), r_id, a_id)
        for r_id, r_start in runna
        for a_id, a_start in apple
        if abs((r_start - a_start).total_seconds()) <= window_s
    )
    used_apple: set[ActivityId] = set()
    for delta, runna_id, apple_id in candidates:
        if apple_id in used_apple:
            continue
        store.insert_runna_link(
            conn, runna_id, apple_id, round(1 - delta / window_s, 3)
        )
        used_apple.add(apple_id)
    return len(used_apple)
