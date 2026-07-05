"""End-to-end test for bulk-archive ingest against a real ZIP."""

from __future__ import annotations

import gzip
import zipfile
from pathlib import Path

from runlog.config import resolve_paths
from runlog.db import store
from runlog.ingest.strava_ingest import import_bulk_archive
from runlog.sources.strava.test_bulk import _CSV
from runlog.sources.test_gpx import _GPX


def _build_archive(zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("activities.csv", _CSV)
        archive.writestr("activities/123.gpx.gz", gzip.compress(_GPX))


def test_import_bulk_archive_populates_db(tmp_path: Path) -> None:
    zip_path = tmp_path / "export.zip"
    _build_archive(zip_path)
    paths = resolve_paths(tmp_path / "data")
    conn = store.connect(paths.db_path)
    store.init_db(conn)

    stored = import_bulk_archive(conn, paths, zip_path)

    counts = store.table_counts(conn)
    assert (
        stored,
        counts["activities"],
        counts["stream_points"],
        counts["raw_files"],
    ) == (
        2,
        2,
        2,
        1,
    )


def test_import_bulk_archive_prefers_gpx_distance(tmp_path: Path) -> None:
    zip_path = tmp_path / "export.zip"
    _build_archive(zip_path)
    paths = resolve_paths(tmp_path / "data")
    conn = store.connect(paths.db_path)
    store.init_db(conn)
    import_bulk_archive(conn, paths, zip_path)

    # Activity 123 has a GPX track (~111 m); its CSV distance was 10 km. The
    # GPX-derived distance must win. Activity 124 (no track) keeps CSV distance.
    distances = {
        row["source_id"]: row["distance_m"]
        for row in conn.execute("SELECT source_id, distance_m FROM activities")
    }
    assert distances["124"] == 5000.0
    assert distances["123"] < 200.0
