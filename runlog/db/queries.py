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
