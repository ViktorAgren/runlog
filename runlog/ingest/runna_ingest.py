"""Ingest a Runna export ZIP into the local store.

The ZIP is archived verbatim, then ``activities.json`` is parsed for native
recordings and each run's ``fit-files/<id>.fit`` supplies the per-second stream
and workout laps. Indoor runs carry Runna's manually set treadmill distance, so
Runna is the authoritative source for them. Each activity records an exact link
to its Strava twin (when already imported) so the duplicate collapses to one run.
"""

from __future__ import annotations

import zipfile
from typing import TYPE_CHECKING

from runlog.db import store
from runlog.domain import Activity, ActivityId, ActivityRecord, SourceId
from runlog.ingest import archive, link
from runlog.sources.runna import export, fit

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    from runlog.config import Paths
    from runlog.domain import Lap, StreamPoint
    from runlog.sources.runna.export import RunnaActivity


def _pace_s_per_km(duration_s: int | None, distance_m: float | None) -> float | None:
    if not duration_s or not distance_m or distance_m <= 0:
        return None
    return duration_s / (distance_m / 1000)


def _build_activity(activity: RunnaActivity, raw_path: str) -> Activity:
    return Activity(
        source="runna",
        source_id=SourceId(activity.runna_id),
        sport_type="Run",
        start_time_utc=activity.start_time_utc,
        elapsed_s=activity.elapsed_s,
        moving_s=activity.moving_s,
        distance_m=activity.distance_m,
        avg_hr=activity.avg_hr,
        max_hr=activity.max_hr,
        avg_pace_s_per_km=_pace_s_per_km(
            activity.moving_s or activity.elapsed_s, activity.distance_m
        ),
        avg_cadence=activity.avg_cadence,
        calories=activity.calories,
        name=activity.name,
        raw_path=raw_path,
        is_indoor=activity.is_indoor,
        max_speed_mps=activity.max_speed_mps,
        avg_running_speed_mps=activity.avg_speed_mps,
    )


def _find_member(names: list[str], suffix: str) -> str | None:
    return next((name for name in names if name.endswith(suffix)), None)


def _load_fit(
    export_zip: zipfile.ZipFile, names: list[str], activity: RunnaActivity
) -> tuple[tuple[StreamPoint, ...], tuple[Lap, ...]]:
    member = _find_member(names, f"fit-files/{activity.fit_file}")
    if member is None:
        return (), ()
    parsed = fit.parse_fit(export_zip.read(member))
    return parsed.stream, parsed.laps


def _link_strava_twin(
    conn: sqlite3.Connection, runna_id: ActivityId, strava_activity_id: int
) -> None:
    row = conn.execute(
        "SELECT id FROM activities WHERE source = 'strava' AND source_id = ?",
        (str(strava_activity_id),),
    ).fetchone()
    if row is not None:
        store.insert_runna_link(conn, runna_id, ActivityId(int(row["id"])), 1.0)


def import_export(conn: sqlite3.Connection, paths: Paths, zip_path: Path) -> int:
    """Ingest a Runna export ZIP. Returns the number of activities stored.

    Run after Strava so each activity's twin exists to link; re-import is
    idempotent and fills links for twins that arrived later.
    """
    raw_bytes = zip_path.read_bytes()
    raw_path = archive.write_raw(
        conn,
        paths,
        kind="runna",
        source="runna",
        source_id=SourceId(f"export:{zip_path.name}"),
        filename=zip_path.name,
        data=raw_bytes,
    )

    stored = 0
    with zipfile.ZipFile(zip_path) as export_zip:
        names = export_zip.namelist()
        member = _find_member(names, "activities.json")
        if member is None:
            raise ValueError("Runna export does not contain activities.json")
        for activity in export.parse_activities(export_zip.read(member)):
            stream, laps = _load_fit(export_zip, names, activity)
            activity_id = store.store_record(
                conn,
                ActivityRecord(
                    activity=_build_activity(activity, str(raw_path)),
                    laps=laps,
                    stream=stream,
                ),
            )
            if activity.strava_activity_id is not None:
                _link_strava_twin(conn, activity_id, activity.strava_activity_id)
            stored += 1
    link.link_runna_apple(conn)
    return stored
