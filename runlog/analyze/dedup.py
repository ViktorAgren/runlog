"""Cross-source de-duplication of the same physical run.

The same run can land in Strava, Apple Health and Runna. This clusters a run's
rows across all three (union-find over the link tables) and picks one canonical
row per cluster, so run-level analytics never double-count. Pure graph logic and
data access returning primitives; ``metrics.canonical_run_activities`` builds the
typed rows from the result.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from runlog.db import queries

if TYPE_CHECKING:
    import sqlite3

# Preferred canonical source for an outdoor cluster: Strava (GPS) over Apple over
# Runna. Indoor clusters override this to prefer Runna's manual treadmill distance.
_SOURCE_PRIORITY: dict[str, int] = {"strava": 0, "apple_health": 1, "runna": 2}

# Running-dynamics fields that exist only on the Apple side of a linked pair.
_APPLE_DYNAMICS = (
    "avg_power_w",
    "avg_stride_length_m",
    "avg_vertical_oscillation_cm",
    "avg_ground_contact_ms",
)


def coalesce(row: sqlite3.Row, twin: dict[str, float], field: str) -> float | None:
    """Row's own value for ``field``, else the linked Apple twin's."""
    value: float | None = row[field]
    return value if value is not None else twin.get(field)


def _apple_dynamics(
    conn: sqlite3.Connection, apple_ids: set[int]
) -> dict[int, dict[str, float]]:
    """Map each linked Apple activity id to its non-null running-dynamics values."""
    if not apple_ids:
        return {}
    placeholders = queries.in_placeholders(len(apple_ids))
    fields = ", ".join(_APPLE_DYNAMICS)
    return {
        int(row["id"]): {
            field: row[field] for field in _APPLE_DYNAMICS if row[field] is not None
        }
        for row in conn.execute(
            f"SELECT id, {fields} FROM activities WHERE id IN ({placeholders})",
            tuple(apple_ids),
        )
    }


def dedup_clusters(
    conn: sqlite3.Connection,
) -> tuple[set[int], dict[int, dict[str, float]]]:
    """Cluster the same physical run across sources and pick one canonical row.

    Returns the ids to drop (every non-canonical cluster member) and, per kept
    canonical id, the running-dynamics values inherited from its dropped twins.
    """
    edges = [
        (int(row["strava_activity_id"]), int(row["apple_activity_id"]))
        for row in conn.execute(
            "SELECT strava_activity_id, apple_activity_id FROM activity_links"
        )
    ]
    edges += [
        (int(row["runna_activity_id"]), int(row["twin_activity_id"]))
        for row in conn.execute(
            "SELECT runna_activity_id, twin_activity_id FROM runna_links"
        )
    ]
    if not edges:
        return set(), {}
    clusters = _connected_components(edges)
    nodes = {node for cluster in clusters for node in cluster}
    placeholders = queries.in_placeholders(len(nodes))
    meta = {
        int(row["id"]): (str(row["source"]), bool(row["is_indoor"]))
        for row in conn.execute(
            f"SELECT id, source, is_indoor FROM activities "
            f"WHERE id IN ({placeholders})",
            tuple(nodes),
        )
    }
    dynamics = _apple_dynamics(conn, nodes)
    superseded: set[int] = set()
    inherited: dict[int, dict[str, float]] = {}
    for cluster in clusters:
        canonical = _pick_canonical(cluster, meta)
        twins = [node for node in cluster if node != canonical]
        superseded.update(twins)
        merged: dict[str, float] = {}
        for twin in twins:
            for field, value in dynamics.get(twin, {}).items():
                merged.setdefault(field, value)
        if merged:
            inherited[canonical] = merged
    return superseded, inherited


def _connected_components(edges: list[tuple[int, int]]) -> list[list[int]]:
    """Union-find over link edges: each component is one physical run."""
    parent: dict[int, int] = {}

    def find(node: int) -> int:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for left, right in edges:
        parent[find(left)] = find(right)
    groups: dict[int, list[int]] = defaultdict(list)
    for node in parent:
        groups[find(node)].append(node)
    return list(groups.values())


def _pick_canonical(cluster: list[int], meta: dict[int, tuple[str, bool]]) -> int:
    """Canonical row for a cluster: an indoor Runna row wins, else by source."""
    indoor_runna = [i for i in cluster if meta[i][0] == "runna" and meta[i][1]]
    if indoor_runna:
        return min(indoor_runna)
    return min(cluster, key=lambda i: (_SOURCE_PRIORITY.get(meta[i][0], 9), i))
