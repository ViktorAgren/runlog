"""Unit tests for the shared GPX parser."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from runlog.sources.gpx import parse_gpx

_GPX = b"""<?xml version="1.0" encoding="UTF-8"?>
<gpx version="1.1" creator="StravaGPX" xmlns="http://www.topografix.com/GPX/1/1"
     xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">
 <trk><name>Morning Run</name>
  <trkseg>
   <trkpt lat="59.3000" lon="18.0000"><ele>10.0</ele>
     <time>2026-06-01T07:30:00Z</time>
     <extensions><gpxtpx:TrackPointExtension>
       <gpxtpx:hr>120</gpxtpx:hr><gpxtpx:cad>80</gpxtpx:cad>
     </gpxtpx:TrackPointExtension></extensions></trkpt>
   <trkpt lat="59.3010" lon="18.0000"><ele>12.0</ele>
     <time>2026-06-01T07:30:10Z</time>
     <extensions><gpxtpx:TrackPointExtension>
       <gpxtpx:hr>130</gpxtpx:hr><gpxtpx:cad>82</gpxtpx:cad>
     </gpxtpx:TrackPointExtension></extensions></trkpt>
  </trkseg>
 </trk>
</gpx>
"""


def test_parse_gpx_extracts_stream_points() -> None:
    track = parse_gpx(_GPX)
    assert [(p.offset_s, p.hr, p.cadence) for p in track.points] == [
        (0, 120.0, 80.0),
        (10, 130.0, 82.0),
    ]


def test_parse_gpx_derives_summary() -> None:
    track = parse_gpx(_GPX)
    assert (
        track.start_time_utc,
        track.avg_hr,
        track.max_hr,
        track.elevation_gain_m,
    ) == (datetime(2026, 6, 1, 7, 30, tzinfo=UTC), 125.0, 130.0, 2.0)


def test_parse_gpx_accumulates_distance() -> None:
    # ~0.001 deg of latitude at this location is ~111 m.
    track = parse_gpx(_GPX)
    assert track.distance_m == pytest.approx(111.0, abs=5.0)
