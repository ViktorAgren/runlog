"""Unit tests for persisting and exporting the derived-metrics layer."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from runlog.analyze import persist
from runlog.analyze.series import Scalar, Series
from runlog.db import queries, store


def _db() -> sqlite3.Connection:
    conn = store.connect(":memory:")  # type: ignore[arg-type]
    store.init_db(conn)
    return conn


def test_persist_writes_series_and_scalars() -> None:
    conn = _db()
    series = [
        Series(
            name="ctl",
            lane="training",
            unit="load",
            points=[(date(2026, 1, 1), 10.0), (date(2026, 1, 2), 12.0)],
        )
    ]
    scalars = [Scalar(name="vo2max", value=61.6, unit="ml/kg/min")]
    persist.persist(conn, series, scalars, "2026-01-03T00:00:00")
    assert queries.read_series(conn, "ctl") == (
        "training",
        "load",
        [("2026-01-01", 10.0), ("2026-01-02", 12.0)],
    )
    assert queries.all_scalars(conn) == [("vo2max", 61.6, "ml/kg/min")]


def test_export_writes_tidy_csv(tmp_path: Path) -> None:
    conn = _db()
    persist.persist(
        conn,
        [
            Series(
                name="ctl",
                lane="training",
                unit="load",
                points=[(date(2026, 1, 1), 10.0)],
            )
        ],
        [Scalar(name="v", value=1.5, unit="u")],
        "t",
    )
    paths = persist.export(conn, tmp_path)
    assert [p.name for p in paths] == ["series.csv", "scalars.csv"]
    assert (tmp_path / "series.csv").read_text(encoding="utf-8").splitlines() == [
        "name,lane,unit,day,value",
        "ctl,training,load,2026-01-01,10.0",
    ]
    assert (tmp_path / "scalars.csv").read_text(encoding="utf-8").splitlines() == [
        "name,value,unit",
        "v,1.5,u",
    ]
