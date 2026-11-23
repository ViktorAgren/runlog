"""Pure graph and canonical-selection logic for cross-source de-duplication."""

from __future__ import annotations

import sqlite3

from runlog.analyze import dedup


def _row(avg_power_w: float | None) -> sqlite3.Row:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE t (avg_power_w REAL)")
    conn.execute("INSERT INTO t VALUES (?)", (avg_power_w,))
    row: sqlite3.Row = conn.execute("SELECT * FROM t").fetchone()
    return row


def test_coalesce_prefers_row_value() -> None:
    assert dedup.coalesce(_row(210.0), {"avg_power_w": 250.0}, "avg_power_w") == 210.0


def test_coalesce_falls_back_to_twin() -> None:
    assert dedup.coalesce(_row(None), {"avg_power_w": 250.0}, "avg_power_w") == 250.0


def test_coalesce_none_when_neither_has_it() -> None:
    assert dedup.coalesce(_row(None), {}, "avg_power_w") is None


def _components(edges: list[tuple[int, int]]) -> set[frozenset[int]]:
    return {frozenset(group) for group in dedup._connected_components(edges)}


def test_connected_components_separates_unlinked_runs() -> None:
    assert _components([(1, 2), (3, 4)]) == {frozenset({1, 2}), frozenset({3, 4})}


def test_connected_components_chains_through_a_shared_twin() -> None:
    # A Runna run (10) linked to two Apple twins (11, 12), each with its own
    # Strava row (1, 2), collapses into one physical-run cluster; an unrelated
    # pair (20, 21) stays separate. Documents the time-bounded chaining the
    # link window permits.
    edges = [(10, 11), (10, 12), (11, 1), (12, 2), (20, 21)]
    assert _components(edges) == {frozenset({1, 2, 10, 11, 12}), frozenset({20, 21})}


def test_pick_canonical_prefers_indoor_runna() -> None:
    meta = {1: ("strava", False), 2: ("runna", True)}
    assert dedup._pick_canonical([1, 2], meta) == 2


def test_pick_canonical_uses_lowest_indoor_runna_id() -> None:
    meta = {5: ("runna", True), 3: ("runna", True)}
    assert dedup._pick_canonical([5, 3], meta) == 3


def test_pick_canonical_outdoor_prefers_strava_over_lower_id() -> None:
    meta = {2: ("apple_health", False), 1: ("runna", False), 4: ("strava", False)}
    assert dedup._pick_canonical([2, 1, 4], meta) == 4


def test_pick_canonical_tie_breaks_on_lowest_id() -> None:
    meta = {7: ("strava", False), 3: ("strava", False)}
    assert dedup._pick_canonical([7, 3], meta) == 3
