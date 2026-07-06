"""End-to-end test for Apple Health export ingest against a real ZIP."""

from __future__ import annotations

import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from runlog.config import resolve_paths
from runlog.db import store
from runlog.domain import Activity, ActivityRecord, SourceId, StreamPoint
from runlog.ingest.apple_ingest import (
    _window_mean,
    attach_heart_rate,
    attach_run_dynamics,
    import_export,
)
from runlog.sources.apple_health.test_export import _EXPORT_XML
from runlog.sources.test_gpx import _GPX


def test_window_mean_averages_only_samples_inside_window() -> None:
    start = datetime(2026, 6, 1, 7, 0, tzinfo=UTC)
    times = [start + timedelta(seconds=s) for s in (5, 30, 55, 120)]
    values = [200.0, 210.0, 220.0, 999.0]  # 999 is outside the 60 s window
    assert _window_mean(times, values, start, start + timedelta(seconds=60)) == 210.0


def test_attach_run_dynamics_fills_strava_run_from_standalone_samples() -> None:
    conn = store.connect(":memory:")  # type: ignore[arg-type]
    store.init_db(conn)
    start = datetime(2026, 6, 1, 7, 0, tzinfo=UTC)
    run_id = store.store_record(
        conn,
        ActivityRecord(
            activity=Activity(
                source="strava",
                source_id=SourceId("strava:1"),
                sport_type="Run",
                start_time_utc=start,
                elapsed_s=60,
            ),
        ),
    )
    samples = {
        "avg_power_w": (
            (start + timedelta(seconds=10), 200.0),
            (start + timedelta(seconds=50), 220.0),
            (start + timedelta(seconds=120), 999.0),  # after the run ends
        )
    }
    updated = attach_run_dynamics(conn, samples)
    row = conn.execute(
        "SELECT avg_power_w FROM activities WHERE id = ?", (int(run_id),)
    ).fetchone()
    assert (updated, row["avg_power_w"]) == (1, 210.0)


def test_attach_heart_rate_matches_nearest_within_tolerance() -> None:
    start = datetime(2026, 6, 1, 7, 0, tzinfo=UTC)
    base = start.timestamp()
    points = (
        StreamPoint(offset_s=0),
        StreamPoint(offset_s=10),
        StreamPoint(offset_s=20),
        StreamPoint(offset_s=100),  # no sample within 30 s
    )
    times = [base + 2, base + 12, base + 25]
    hr = [150.0, 160.0, 155.0]
    enriched = attach_heart_rate(points, start, times, hr)
    assert [p.hr for p in enriched] == [150.0, 160.0, 155.0, None]


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
