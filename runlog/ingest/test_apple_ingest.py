"""End-to-end test for Apple Health export ingest against a real ZIP."""

from __future__ import annotations

import zipfile
from pathlib import Path

from runlog.config import resolve_paths
from runlog.db import store
from runlog.ingest.apple_ingest import import_export
from runlog.sources.apple_health.test_export import _EXPORT_XML
from runlog.sources.test_gpx import _GPX

_ROUTE = "apple_health_export/workout-routes/route_2026-06-01_7.30am.gpx"


def _build_export(zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w") as export:
        export.writestr("apple_health_export/export.xml", _EXPORT_XML)
        export.writestr(_ROUTE, _GPX)


def test_import_export_stores_workout_metrics_and_route(tmp_path: Path) -> None:
    zip_path = tmp_path / "export.zip"
    _build_export(zip_path)
    paths = resolve_paths(tmp_path / "data")
    conn = store.connect(paths.db_path)
    store.init_db(conn)

    workouts, metrics = import_export(conn, paths, zip_path)

    counts = store.table_counts(conn)
    assert (workouts, metrics, counts) == (
        1,
        3,
        {
            "activities": 1,
            "laps": 2,
            "stream_points": 2,
            "health_metrics": 3,
            "activity_links": 0,
            "raw_files": 1,
            "activities:apple_health": 1,
        },
    )


def test_import_export_is_idempotent(tmp_path: Path) -> None:
    zip_path = tmp_path / "export.zip"
    _build_export(zip_path)
    paths = resolve_paths(tmp_path / "data")
    conn = store.connect(paths.db_path)
    store.init_db(conn)

    import_export(conn, paths, zip_path)
    import_export(conn, paths, zip_path)

    assert store.table_counts(conn)["activities"] == 1
