"""Parser for a Strava bulk "Download your data" export.

The archive contains ``activities.csv`` (one summary row per activity) and an
``activities/`` folder of per-activity track files (``*.gpx``/``*.gpx.gz`` and,
for some activities, ``*.fit.gz`` which we do not parse). This module is pure:
it turns CSV text and gzipped track bytes into structured values, leaving all
zip/IO to the ingest layer so it can be unit-tested without a real archive.
"""

from __future__ import annotations

import csv
import gzip
import io
from dataclasses import dataclass
from datetime import UTC, datetime

from runlog.domain import SourceId

# Strava renders bulk-export dates in UTC, e.g. "Jun 1, 2026, 7:30:12 AM".
_DATE_FORMAT = "%b %d, %Y, %I:%M:%S %p"


@dataclass(frozen=True)
class BulkRow:
    """One row of ``activities.csv`` reduced to the fields we store.

    ``track_name`` is the ``activities/``-relative filename of the track file,
    or ``None`` when the export lists no track for the activity.
    """

    activity_id: SourceId
    name: str | None
    sport_type: str
    start_time_utc: datetime
    elapsed_s: int | None
    moving_s: int | None
    distance_m: float | None
    avg_hr: float | None
    max_hr: float | None
    elevation_gain_m: float | None
    calories: float | None
    total_steps: float | None
    track_name: str | None
    relative_effort: float | None = None
    grade_adj_distance_m: float | None = None
    max_speed_mps: float | None = None
    elevation_loss_m: float | None = None
    avg_grade: float | None = None
    max_grade: float | None = None
    avg_watts: float | None = None
    training_load: float | None = None
    intensity: float | None = None
    temp_c: float | None = None
    humidity: float | None = None
    wind_mps: float | None = None


def _num(value: str | None) -> float | None:
    if value is None or value.strip() == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _first_indices(header: list[str], columns: tuple[str, ...]) -> dict[str, int]:
    """Map each wanted column to the index of its first occurrence in ``header``.

    Strava exports repeat some headers (notably ``Distance``); taking the first
    occurrence keeps the summary column and its documented units.
    """
    return {col: header.index(col) for col in columns if col in header}


def _cell(row: list[str], index: int | None) -> str | None:
    if index is None or index >= len(row):
        return None
    value = row[index].strip()
    return value or None


def parse_activities_csv(text: str) -> list[BulkRow]:
    """Parse ``activities.csv`` text into ``BulkRow`` records."""
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return []
    header = rows[0]
    idx = _first_indices(
        header,
        (
            "Activity ID",
            "Activity Date",
            "Activity Name",
            "Activity Type",
            "Elapsed Time",
            "Moving Time",
            "Distance",
            "Max Heart Rate",
            "Average Heart Rate",
            "Elevation Gain",
            "Calories",
            "Total Steps",
            "Relative Effort",
            "Grade Adjusted Distance",
            "Max Speed",
            "Elevation Loss",
            "Average Grade",
            "Max Grade",
            "Average Watts",
            "Training Load",
            "Intensity",
            "Weather Temperature",
            "Humidity",
            "Wind Speed",
            "Filename",
        ),
    )

    parsed: list[BulkRow] = []
    for row in rows[1:]:
        activity_id = _cell(row, idx.get("Activity ID"))
        if activity_id is None:
            continue
        distance_km = _num(_cell(row, idx.get("Distance")))
        elapsed = _num(_cell(row, idx.get("Elapsed Time")))
        moving = _num(_cell(row, idx.get("Moving Time")))
        parsed.append(
            BulkRow(
                activity_id=SourceId(activity_id),
                name=_cell(row, idx.get("Activity Name")),
                sport_type=_cell(row, idx.get("Activity Type")) or "Unknown",
                start_time_utc=_parse_date(_cell(row, idx.get("Activity Date"))),
                elapsed_s=int(elapsed) if elapsed is not None else None,
                moving_s=int(moving) if moving is not None else None,
                distance_m=distance_km * 1000 if distance_km is not None else None,
                avg_hr=_num(_cell(row, idx.get("Average Heart Rate"))),
                max_hr=_num(_cell(row, idx.get("Max Heart Rate"))),
                elevation_gain_m=_num(_cell(row, idx.get("Elevation Gain"))),
                calories=_num(_cell(row, idx.get("Calories"))),
                total_steps=_num(_cell(row, idx.get("Total Steps"))),
                relative_effort=_num(_cell(row, idx.get("Relative Effort"))),
                grade_adj_distance_m=_num(
                    _cell(row, idx.get("Grade Adjusted Distance"))
                ),
                max_speed_mps=_num(_cell(row, idx.get("Max Speed"))),
                elevation_loss_m=_num(_cell(row, idx.get("Elevation Loss"))),
                avg_grade=_num(_cell(row, idx.get("Average Grade"))),
                max_grade=_num(_cell(row, idx.get("Max Grade"))),
                avg_watts=_num(_cell(row, idx.get("Average Watts"))),
                training_load=_num(_cell(row, idx.get("Training Load"))),
                intensity=_num(_cell(row, idx.get("Intensity"))),
                temp_c=_num(_cell(row, idx.get("Weather Temperature"))),
                humidity=_num(_cell(row, idx.get("Humidity"))),
                wind_mps=_num(_cell(row, idx.get("Wind Speed"))),
                track_name=_track_name(_cell(row, idx.get("Filename"))),
            )
        )
    return parsed


def _parse_date(value: str | None) -> datetime:
    if value is None:
        raise ValueError("activities.csv row is missing 'Activity Date'")
    return datetime.strptime(value, _DATE_FORMAT).replace(tzinfo=UTC)


def _track_name(filename: str | None) -> str | None:
    """Return the ``activities/``-relative name, or ``None`` if absent."""
    if not filename:
        return None
    return filename.split("activities/", 1)[-1]


def is_gpx(track_name: str) -> bool:
    """True if the track file is GPX (possibly gzipped) rather than FIT/TCX."""
    return track_name.endswith((".gpx", ".gpx.gz"))


def decompress_track(track_name: str, data: bytes) -> bytes:
    """Return the raw track bytes, gunzipping when the name ends in ``.gz``."""
    return gzip.decompress(data) if track_name.endswith(".gz") else data
