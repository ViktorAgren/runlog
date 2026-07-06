"""Ingest an Apple Health ``export.zip`` into the local store.

The ZIP is archived verbatim, then ``export.xml`` is streamed for workouts and
curated health metrics. Each workout becomes an ``ActivityRecord`` (its GPX
route, when present, supplies the per-point stream). Workout identity is
synthesized from activity type + start time so re-imports are idempotent.
"""

from __future__ import annotations

import bisect
import dataclasses
import zipfile
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from runlog.db import store
from runlog.domain import Activity, ActivityRecord, SourceId, StreamPoint
from runlog.ingest import archive
from runlog.sources import gpx
from runlog.sources.apple_health.export import parse_export

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from runlog.config import Paths
    from runlog.sources.apple_health.export import AppleWorkout

# Attach an HR sample to a route point only if within this many seconds.
_HR_MATCH_TOLERANCE_S = 30.0

# Sport types whose dynamics get filled from standalone Apple samples.
_RUN_SPORTS = ("Run", "Running")


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


def attach_heart_rate(
    points: Sequence[StreamPoint],
    start_time_utc: datetime,
    sample_times: Sequence[float],
    sample_hr: Sequence[float],
) -> tuple[StreamPoint, ...]:
    """Fill each route point's ``hr`` from the nearest HR sample in time.

    ``sample_times`` are epoch seconds (sorted), ``sample_hr`` the matching bpm.
    A point keeps ``hr=None`` if no sample lies within the match tolerance.
    """
    if not points or not sample_times:
        return tuple(points)
    base = start_time_utc.timestamp()
    enriched: list[StreamPoint] = []
    for point in points:
        target = base + point.offset_s
        i = bisect.bisect_left(sample_times, target)
        best_hr: float | None = None
        best_gap = _HR_MATCH_TOLERANCE_S
        for j in (i - 1, i):
            if 0 <= j < len(sample_times):
                gap = abs(sample_times[j] - target)
                if gap <= best_gap:
                    best_gap = gap
                    best_hr = sample_hr[j]
        enriched.append(
            dataclasses.replace(point, hr=best_hr) if best_hr is not None else point
        )
    return tuple(enriched)


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
        avg_power_w=workout.avg_power_w,
        avg_stride_length_m=workout.avg_stride_length_m,
        avg_vertical_oscillation_cm=workout.avg_vertical_oscillation_cm,
        avg_ground_contact_ms=workout.avg_ground_contact_ms,
        avg_running_speed_mps=workout.avg_running_speed_mps,
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

        sample_times = [t.timestamp() for t, _ in export.heart_rate_samples]
        sample_hr = [hr for _, hr in export.heart_rate_samples]
        metric_count = store.insert_health_metrics(conn, export.metrics)
        for workout in export.workouts:
            stream_points = _load_route(export_zip, names, workout)
            if stream_points and not any(p.hr is not None for p in stream_points):
                stream_points = attach_heart_rate(
                    stream_points, workout.start_time_utc, sample_times, sample_hr
                )
            store.store_record(
                conn,
                ActivityRecord(
                    activity=_build_activity(workout, str(raw_path)),
                    laps=workout.laps,
                    stream=stream_points,
                ),
            )

    attach_run_dynamics(conn, export.dynamics_samples)
    return len(export.workouts), metric_count


def _window_mean(
    times: Sequence[datetime],
    values: Sequence[float],
    start: datetime,
    end: datetime,
) -> float | None:
    """Mean of ``values`` whose (sorted) ``times`` fall in ``[start, end]``."""
    lo = bisect.bisect_left(times, start)
    hi = bisect.bisect_right(times, end)
    if lo >= hi:
        return None
    return sum(values[lo:hi]) / (hi - lo)


def attach_run_dynamics(
    conn: sqlite3.Connection,
    dynamics_samples: Mapping[str, Sequence[tuple[datetime, float]]],
) -> int:
    """Fill run dynamics columns by averaging standalone samples over each run.

    Newer exports store running power/stride/etc. as standalone per-second
    records rather than workout statistics, so runs logged via Strava carry
    none. For every run activity we average the samples inside its
    ``[start, start+duration]`` window and write the result. Returns the number
    of activities updated. Runs must already be imported (call after Strava).
    """
    if not dynamics_samples:
        return 0
    prepared = {
        column: ([t for t, _ in samples], [v for _, v in samples])
        for column, samples in dynamics_samples.items()
    }
    placeholders = ",".join("?" for _ in _RUN_SPORTS)
    updated = 0
    for row in conn.execute(
        f"SELECT id, start_time_utc, elapsed_s, moving_s FROM activities "
        f"WHERE sport_type IN ({placeholders})",
        _RUN_SPORTS,
    ).fetchall():
        duration_s = row["elapsed_s"] or row["moving_s"]
        if not duration_s:
            continue
        start = datetime.fromisoformat(row["start_time_utc"])
        end = start + timedelta(seconds=duration_s)
        assignments = {
            column: round(mean, 3)
            for column, (times, values) in prepared.items()
            if (mean := _window_mean(times, values, start, end)) is not None
        }
        if assignments:
            clause = ", ".join(f"{column} = ?" for column in assignments)
            conn.execute(
                f"UPDATE activities SET {clause} WHERE id = ?",
                (*assignments.values(), row["id"]),
            )
            updated += 1
    conn.commit()
    return updated


def _load_route(
    export_zip: zipfile.ZipFile, names: list[str], workout: AppleWorkout
) -> tuple[StreamPoint, ...]:
    if workout.route_file is None:
        return ()
    member = _find_member(names, f"workout-routes/{workout.route_file}")
    if member is None:
        return ()
    return gpx.parse_gpx(export_zip.read(member)).points
