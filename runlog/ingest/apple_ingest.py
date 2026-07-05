"""Ingest an Apple Health ``export.zip`` into the local store.

The ZIP is archived verbatim, then ``export.xml`` is streamed for workouts and
curated health metrics. Each workout becomes an ``ActivityRecord`` (its GPX
route, when present, supplies the per-point stream). Workout identity is
synthesized from activity type + start time so re-imports are idempotent.
"""

from __future__ import annotations

import zipfile
from typing import TYPE_CHECKING

from runlog.db import store
from runlog.domain import Activity, ActivityRecord, SourceId, StreamPoint
from runlog.ingest import archive
from runlog.sources import gpx
from runlog.sources.apple_health.export import parse_export

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    from runlog.config import Paths
    from runlog.sources.apple_health.export import AppleWorkout


def _source_id(workout: AppleWorkout) -> SourceId:
    # Start alone can collide (two workouts of the same type at the same start);
    # including the end time keeps distinct workouts distinct while still
    # deduping identical re-imports.
    return SourceId(
        f"{workout.activity_type}"
        f":{workout.start_time_utc.isoformat()}"
        f":{workout.end_time_utc.isoformat()}"
    )


def _pace_s_per_km(duration_s: int | None, distance_m: float | None) -> float | None:
    if not duration_s or not distance_m or distance_m <= 0:
        return None
    return duration_s / (distance_m / 1000)


def _find_member(names: list[str], suffix: str) -> str | None:
    return next((name for name in names if name.endswith(suffix)), None)


def _build_activity(workout: AppleWorkout, raw_path: str) -> Activity:
    return Activity(
        source="apple_health",
        source_id=_source_id(workout),
        sport_type=workout.activity_type,
        start_time_utc=workout.start_time_utc,
        elapsed_s=workout.duration_s,
        moving_s=workout.duration_s,
        distance_m=workout.distance_m,
        avg_hr=workout.avg_hr,
        max_hr=workout.max_hr,
        avg_pace_s_per_km=_pace_s_per_km(workout.duration_s, workout.distance_m),
        avg_cadence=workout.avg_cadence,
        calories=workout.calories,
        raw_path=raw_path,
    )


def import_export(
    conn: sqlite3.Connection, paths: Paths, zip_path: Path
) -> tuple[int, int]:
    """Ingest an Apple Health export. Returns (workouts stored, metrics stored)."""
    raw_bytes = zip_path.read_bytes()
    raw_path = archive.write_raw(
        conn,
        paths,
        kind="apple_health",
        source="apple_health",
        source_id=SourceId(f"export:{zip_path.name}"),
        filename=zip_path.name,
        data=raw_bytes,
    )

    with zipfile.ZipFile(zip_path) as export_zip:
        names = export_zip.namelist()
        xml_member = _find_member(names, "export.xml")
        if xml_member is None:
            raise ValueError("export.zip does not contain export.xml")
        with export_zip.open(xml_member) as stream:
            export = parse_export(stream)

        metric_count = store.insert_health_metrics(conn, export.metrics)
        for workout in export.workouts:
            stream_points = _load_route(export_zip, names, workout)
            store.store_record(
                conn,
                ActivityRecord(
                    activity=_build_activity(workout, str(raw_path)),
                    laps=workout.laps,
                    stream=stream_points,
                ),
            )

    return len(export.workouts), metric_count


def _load_route(
    export_zip: zipfile.ZipFile, names: list[str], workout: AppleWorkout
) -> tuple[StreamPoint, ...]:
    if workout.route_file is None:
        return ()
    member = _find_member(names, f"workout-routes/{workout.route_file}")
    if member is None:
        return ()
    return gpx.parse_gpx(export_zip.read(member)).points
