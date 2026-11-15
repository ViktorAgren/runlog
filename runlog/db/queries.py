"""Read queries over the runlog database, kept out of the analysis layer.

Pure data access: each function takes a connection and returns primitives, so
the analytics modules can stay free of embedded SQL.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence

    from runlog.domain import ActivityId


def in_placeholders(count: int) -> str:
    """A ``?,?,...`` list of ``count`` placeholders for a SQL ``IN`` clause."""
    return ",".join("?" for _ in range(count))


def hr_samples(conn: sqlite3.Connection, run_ids: Sequence[ActivityId]) -> list[float]:
    """All per-point heart-rate samples for the given runs (for a histogram)."""
    if not run_ids:
        return []
    return [
        float(row["hr"])
        for row in conn.execute(
            f"""
            SELECT hr FROM stream_points
            WHERE hr IS NOT NULL AND activity_id IN ({in_placeholders(len(run_ids))})
            """,
            tuple(int(i) for i in run_ids),
        )
    ]


# --- Derived analytics store (latest-wins) ----------------------------------


def replace_series(
    conn: sqlite3.Connection,
    name: str,
    lane: str,
    unit: str,
    rows: Sequence[tuple[str, float]],
    computed_at: str,
) -> None:
    """Replace a named series with ``rows`` of ``(iso_day, value)`` (latest-wins)."""
    conn.execute("DELETE FROM derived_series WHERE name = ?", (name,))
    conn.executemany(
        "INSERT INTO derived_series "
        "(name, seq, lane, unit, day, value, computed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (name, seq, lane, unit, day, value, computed_at)
            for seq, (day, value) in enumerate(rows)
        ],
    )


def upsert_scalar(
    conn: sqlite3.Connection, name: str, value: float, unit: str, computed_at: str
) -> None:
    """Insert or replace a single named derived scalar."""
    conn.execute(
        "INSERT OR REPLACE INTO derived_scalars (name, value, unit, computed_at) "
        "VALUES (?, ?, ?, ?)",
        (name, value, unit, computed_at),
    )


def read_series(
    conn: sqlite3.Connection, name: str
) -> tuple[str, str, list[tuple[str, float]]] | None:
    """A stored series as ``(lane, unit, [(iso_day, value)])``, or None if absent."""
    rows = conn.execute(
        "SELECT lane, unit, day, value FROM derived_series WHERE name = ? ORDER BY seq",
        (name,),
    ).fetchall()
    if not rows:
        return None
    return (
        str(rows[0]["lane"]),
        str(rows[0]["unit"]),
        [(str(row["day"]), float(row["value"])) for row in rows],
    )


def all_series_rows(
    conn: sqlite3.Connection,
) -> list[tuple[str, str, str, str, float]]:
    """Every stored series row as ``(name, lane, unit, iso_day, value)``."""
    return [
        (
            str(row["name"]),
            str(row["lane"]),
            str(row["unit"]),
            str(row["day"]),
            float(row["value"]),
        )
        for row in conn.execute(
            "SELECT name, lane, unit, day, value FROM derived_series "
            "ORDER BY name, seq"
        )
    ]


def all_scalars(conn: sqlite3.Connection) -> list[tuple[str, float, str]]:
    """Every stored scalar as ``(name, value, unit)``."""
    return [
        (str(row["name"]), float(row["value"]), str(row["unit"]))
        for row in conn.execute(
            "SELECT name, value, unit FROM derived_scalars ORDER BY name"
        )
    ]
