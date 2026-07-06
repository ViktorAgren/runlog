"""Unit tests for the Strava bulk-export CSV/track parser."""

from __future__ import annotations

import gzip
from datetime import UTC, datetime

import pytest

from runlog.domain import SourceId
from runlog.sources.strava.bulk import (
    BulkRow,
    decompress_track,
    is_gpx,
    parse_activities_csv,
)

# Header repeats "Distance": the first (km) is the summary column we want; the
# trailing one (meters) must be ignored.
_CSV = (
    '"Activity ID","Activity Date","Activity Name","Activity Type",'
    '"Elapsed Time","Moving Time","Distance","Max Heart Rate",'
    '"Average Heart Rate","Elevation Gain","Calories","Total Steps",'
    '"Filename","Distance"\n'
    '"123","Jun 1, 2026, 7:30:00 AM","Morning Run","Run","600","580","10.0",'
    '"150","140","20","500","1740","activities/123.gpx.gz","10000"\n'
    '"124","Jun 2, 2026, 6:00:00 AM","Treadmill","Run","1200","1200","5.0",'
    '"160","150","0","400","","","5000"\n'
)


def test_parse_activities_csv_maps_rows() -> None:
    assert parse_activities_csv(_CSV) == [
        BulkRow(
            activity_id=SourceId("123"),
            name="Morning Run",
            sport_type="Run",
            start_time_utc=datetime(2026, 6, 1, 7, 30, tzinfo=UTC),
            elapsed_s=600,
            moving_s=580,
            distance_m=10000.0,
            avg_hr=140.0,
            max_hr=150.0,
            elevation_gain_m=20.0,
            calories=500.0,
            total_steps=1740.0,
            track_name="123.gpx.gz",
        ),
        BulkRow(
            activity_id=SourceId("124"),
            name="Treadmill",
            sport_type="Run",
            start_time_utc=datetime(2026, 6, 2, 6, 0, tzinfo=UTC),
            elapsed_s=1200,
            moving_s=1200,
            distance_m=5000.0,
            avg_hr=150.0,
            max_hr=160.0,
            elevation_gain_m=0.0,
            calories=400.0,
            total_steps=None,
            track_name=None,
        ),
    ]


def test_parse_activities_csv_reads_extra_fields() -> None:
    csv_text = (
        '"Activity ID","Activity Date","Activity Type","Distance",'
        '"Relative Effort","Max Speed","Average Grade","Weather Temperature",'
        '"Humidity","Wind Speed"\n'
        '"9","Jun 1, 2026, 7:30:00 AM","Run","10.0",'
        '"56","4.9","0.1","13.0","0.62","3.0"\n'
    )
    row = parse_activities_csv(csv_text)[0]
    assert (
        row.relative_effort,
        row.max_speed_mps,
        row.avg_grade,
        row.temp_c,
        row.humidity,
        row.wind_mps,
    ) == (56.0, 4.9, 0.1, 13.0, 0.62, 3.0)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("123.gpx", True),
        ("123.gpx.gz", True),
        ("123.fit.gz", False),
        ("123.tcx.gz", False),
    ],
)
def test_is_gpx(name: str, expected: bool) -> None:
    assert is_gpx(name) is expected


def test_decompress_track_gunzips_only_gz() -> None:
    payload = b"<gpx></gpx>"
    assert (
        decompress_track("x.gpx.gz", gzip.compress(payload)),
        decompress_track("x.gpx", payload),
    ) == (payload, payload)
