"""Ingest Strava data from either the bulk export ZIP or the OAuth API.

Bulk: the whole export ZIP is archived verbatim, then each ``activities.csv``
row is normalized into an ``ActivityRecord`` (GPX supplies the stream and
authoritative distance/duration/heart-rate; the CSV fills the gaps).

API: a refresh token yields an access token, activities are paged newest-first,
and each detail (plus optional streams) is archived and normalized. Both paths
share the idempotent store, so a re-sync overwrites rather than duplicates.
"""

from __future__ import annotations

import dataclasses
import json
import zipfile
from datetime import UTC
from pathlib import Path
from typing import TYPE_CHECKING

from runlog.db import store
from runlog.domain import Activity, ActivityRecord, SourceId, StreamPoint
from runlog.ingest import archive
from runlog.sources import gpx
from runlog.sources.strava import bulk, fetch
from runlog.sources.strava.auth import refresh_access_token
from runlog.sources.strava.client import StravaClient

if TYPE_CHECKING:
    import sqlite3
    from datetime import datetime

    from runlog.config import Paths, StravaCredentials
    from runlog.sources.gpx import GpxTrack


def _pace_s_per_km(time_s: int | None, distance_m: float | None) -> float | None:
    if not time_s or not distance_m or distance_m <= 0:
        return None
    return time_s / (distance_m / 1000)


def _cadence_spm(total_steps: float | None, seconds: int | None) -> float | None:
    """Full running cadence (steps/min) from total steps over the duration."""
    if not total_steps or not seconds:
        return None
    return round(total_steps / (seconds / 60), 1)


def _merge_activity(
    row: bulk.BulkRow, track: GpxTrack | None, raw_path: str
) -> Activity:
    """Combine a CSV row with an optional GPX track into an ``Activity``."""
    distance_m = (track.distance_m if track else None) or row.distance_m
    elapsed_s = row.elapsed_s or (track.elapsed_s if track else None)
    pace_time = row.moving_s or elapsed_s
    return Activity(
        source="strava",
        source_id=row.activity_id,
        sport_type=row.sport_type,
        start_time_utc=(track.start_time_utc if track else None) or row.start_time_utc,
        elapsed_s=elapsed_s,
        moving_s=row.moving_s,
        distance_m=distance_m,
        avg_hr=row.avg_hr or (track.avg_hr if track else None),
        max_hr=row.max_hr or (track.max_hr if track else None),
        avg_pace_s_per_km=_pace_s_per_km(pace_time, distance_m),
        avg_cadence=_cadence_spm(row.total_steps, row.moving_s or elapsed_s),
        elevation_gain_m=row.elevation_gain_m
        or (track.elevation_gain_m if track else None),
        calories=row.calories,
        name=row.name,
        raw_path=raw_path,
    )


def _load_track(archive_zip: zipfile.ZipFile, row: bulk.BulkRow) -> GpxTrack | None:
    """Parse the GPX track for a row, or ``None`` if absent/not GPX."""
    if row.track_name is None or not bulk.is_gpx(row.track_name):
        return None
    member = f"activities/{row.track_name}"
    if member not in archive_zip.namelist():
        return None
    data = bulk.decompress_track(row.track_name, archive_zip.read(member))
    return gpx.parse_gpx(data)


def import_bulk_archive(conn: sqlite3.Connection, paths: Paths, zip_path: Path) -> int:
    """Archive and ingest a Strava bulk export ZIP. Returns activities stored."""
    raw_bytes = zip_path.read_bytes()
    raw_path = archive.write_raw(
        conn,
        paths,
        kind="strava_bulk",
        source="strava",
        source_id=SourceId(f"bulk:{zip_path.name}"),
        filename=zip_path.name,
        data=raw_bytes,
    )

    stored = 0
    with zipfile.ZipFile(zip_path) as archive_zip:
        rows = bulk.parse_activities_csv(
            archive_zip.read("activities.csv").decode("utf-8")
        )
        for row in rows:
            track = _load_track(archive_zip, row)
            record = ActivityRecord(
                activity=_merge_activity(row, track, str(raw_path)),
                stream=track.points if track else (),
            )
            store.store_record(conn, record)
            stored += 1
    return stored


def _resolve_after_epoch(
    conn: sqlite3.Connection, after: datetime | None
) -> int | None:
    """Choose the ``after`` epoch: explicit value, else newest stored activity."""
    moment = after or store.latest_start(conn, "strava")
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return int(moment.timestamp())


def _ingest_api_activity(
    conn: sqlite3.Connection,
    paths: Paths,
    client: StravaClient,
    source_id: SourceId,
    with_streams: bool,
) -> None:
    """Fetch, archive, and store one activity's detail and optional streams."""
    detail = fetch.fetch_detail(client, source_id)
    raw_path = archive.write_raw(
        conn,
        paths,
        kind="strava_api",
        source="strava",
        source_id=source_id,
        filename=f"{source_id}.json",
        data=json.dumps(detail).encode("utf-8"),
    )
    stream: tuple[StreamPoint, ...] = ()
    if with_streams:
        streams_json = fetch.fetch_streams(client, source_id)
        archive.write_raw(
            conn,
            paths,
            kind="strava_api",
            source="strava",
            source_id=source_id,
            filename=f"{source_id}.streams.json",
            data=json.dumps(streams_json).encode("utf-8"),
        )
        stream = fetch.parse_streams(streams_json)
    activity = dataclasses.replace(fetch.parse_activity(detail), raw_path=str(raw_path))
    store.store_record(
        conn,
        ActivityRecord(activity=activity, laps=fetch.parse_laps(detail), stream=stream),
    )


def sync_api(
    conn: sqlite3.Connection,
    paths: Paths,
    creds: StravaCredentials,
    after: datetime | None = None,
    with_streams: bool = True,
) -> int:
    """Incrementally sync activities from the Strava API. Returns count stored."""
    token = refresh_access_token(creds)
    client = StravaClient(token.access_token)
    after_epoch = _resolve_after_epoch(conn, after)
    stored = 0
    for summary in fetch.iter_activity_summaries(client, after_epoch):
        source_id = SourceId(str(summary["id"]))
        _ingest_api_activity(conn, paths, client, source_id, with_streams)
        stored += 1
    return stored
