"""Persist and export the derived-metrics layer.

Writes the computed :class:`~runlog.analyze.series.Series` / ``Scalar`` into the
derived store (latest-wins) and exports the whole store as tidy long CSVs. The
store is always rebuildable from the raw tables, so this is a cache + an export
surface, never a source of truth.
"""

from __future__ import annotations

import csv
from typing import TYPE_CHECKING

from runlog.db import queries

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence
    from pathlib import Path

    from runlog.analyze.series import Scalar, Series


def persist(
    conn: sqlite3.Connection,
    series: Sequence[Series],
    scalars: Sequence[Scalar],
    computed_at: str,
) -> None:
    """Write every series and scalar into the derived store, then commit."""
    for s in series:
        queries.replace_series(
            conn,
            s.name,
            s.lane,
            s.unit,
            [(day.isoformat(), value) for day, value in s.points],
            computed_at,
        )
    for scalar in scalars:
        queries.upsert_scalar(conn, scalar.name, scalar.value, scalar.unit, computed_at)
    conn.commit()


def export(conn: sqlite3.Connection, out_dir: Path) -> list[Path]:
    """Export the stored series + scalars as ``series.csv`` and ``scalars.csv``."""
    out_dir.mkdir(parents=True, exist_ok=True)
    series_path = out_dir / "series.csv"
    with series_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["name", "lane", "unit", "day", "value"])
        writer.writerows(queries.all_series_rows(conn))
    scalars_path = out_dir / "scalars.csv"
    with scalars_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["name", "value", "unit"])
        writer.writerows(queries.all_scalars(conn))
    return [series_path, scalars_path]
