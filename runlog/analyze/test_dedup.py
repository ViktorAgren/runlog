"""Unit tests for cross-source de-duplication.

Correctness here is load-bearing: a wrong cluster or canonical pick double-counts
a run, which corrupts every load-derived metric (CTL/ATL/TSB, ACWR, readiness)
and the plans built on them. These assert the clustering, canonical selection and
dynamics inheritance against precomputed results.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from runlog.analyze.dedup import (
    _connected_components,
    _pick_canonical,
    coalesce,
    dedup_clusters,
)
from runlog.db import store
from runlog.domain import Activity, ActivityRecord, Source, SourceId


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = store.connect(":memory:")  # type: ignore[arg-type]
    store.init_db(connection)
    return connection


_BASE = datetime(2026, 6, 1, 7, 0, tzinfo=UTC)
_seq = 0


def _add(
    conn: sqlite3.Connection,
    source: Source,
    *,
    indoor: bool = False,
    power: float | None = None,
    stride: float | None = None,
) -> int:
    global _seq
    _seq += 1
    return store.store_record(
        conn,
        ActivityRecord(
            activity=Activity(
                source=source,
                source_id=SourceId(f"{source}:{_seq}"),
                sport_type="Run",
                start_time_utc=_BASE.replace(minute=_seq),
                is_indoor=indoor,
                avg_power_w=power,
                avg_stride_length_m=stride,
            )
        ),
    )


def _link(conn: sqlite3.Connection, strava_id: int, apple_id: int) -> None:
    conn.execute(
        "INSERT INTO activity_links "
        "(strava_activity_id, apple_activity_id, match_confidence) VALUES (?, ?, 1.0)",
        (strava_id, apple_id),
    )


def _runna_link(conn: sqlite3.Connection, runna_id: int, twin_id: int) -> None:
    conn.execute(
        "INSERT INTO runna_links "
        "(runna_activity_id, twin_activity_id, match_confidence) VALUES (?, ?, 1.0)",
        (runna_id, twin_id),
    )


def test_dedup_clusters_collapses_a_three_source_chain(
    conn: sqlite3.Connection,
) -> None:
    strava = _add(conn, "strava")
    apple = _add(conn, "apple_health", power=300.0, stride=1.2)
    runna = _add(conn, "runna")
    _link(conn, strava, apple)
    _runna_link(conn, runna, strava)

    assert dedup_clusters(conn) == (
        {apple, runna},
        {strava: {"avg_power_w": 300.0, "avg_stride_length_m": 1.2}},
    )


def test_dedup_clusters_indoor_runna_wins_over_source_priority(
    conn: sqlite3.Connection,
) -> None:
    strava = _add(conn, "strava", power=250.0)
    runna = _add(conn, "runna", indoor=True)
    _runna_link(conn, runna, strava)

    assert dedup_clusters(conn) == ({strava}, {runna: {"avg_power_w": 250.0}})


def test_dedup_clusters_keeps_disjoint_pairs_separate(
    conn: sqlite3.Connection,
) -> None:
    strava_a = _add(conn, "strava")
    apple_a = _add(conn, "apple_health")
    strava_b = _add(conn, "strava")
    apple_b = _add(conn, "apple_health")
    _link(conn, strava_a, apple_a)
    _link(conn, strava_b, apple_b)

    assert dedup_clusters(conn) == ({apple_a, apple_b}, {})


def test_dedup_clusters_with_no_links_is_empty(conn: sqlite3.Connection) -> None:
    _add(conn, "strava")
    _add(conn, "apple_health")

    assert dedup_clusters(conn) == (set(), {})


def test_connected_components_merges_transitively_and_splits_disjoint() -> None:
    components = _connected_components([(1, 2), (2, 3), (4, 5)])

    assert sorted(sorted(c) for c in components) == [[1, 2, 3], [4, 5]]


def test_pick_canonical_prefers_source_then_indoor_runna_then_id() -> None:
    meta: dict[int, tuple[str, bool]] = {
        1: ("strava", False),
        2: ("apple_health", False),
        3: ("runna", False),
        10: ("strava", True),
        11: ("runna", True),
        5: ("strava", False),
        8: ("strava", False),
    }

    assert [
        _pick_canonical([1, 2, 3], meta),  # strava over apple over runna
        _pick_canonical([10, 11], meta),  # indoor runna overrides priority
        _pick_canonical([8, 5], meta),  # same source -> lowest id
    ] == [1, 11, 5]


def test_coalesce_prefers_own_value_then_falls_back_to_twin(
    conn: sqlite3.Connection,
) -> None:
    missing = _add(conn, "strava")
    present = _add(conn, "strava", power=200.0)
    twin = {"avg_power_w": 280.0}
    rows = {
        int(row["id"]): row
        for row in conn.execute(
            "SELECT * FROM activities WHERE id IN (?, ?)", (missing, present)
        )
    }

    assert [
        coalesce(rows[missing], twin, "avg_power_w"),
        coalesce(rows[present], twin, "avg_power_w"),
    ] == [280.0, 200.0]
