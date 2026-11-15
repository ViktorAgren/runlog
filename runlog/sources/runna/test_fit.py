"""Tests for FIT stream and lap parsing against a real Runna fixture."""

from __future__ import annotations

from pathlib import Path

from runlog.domain import Lap, StreamPoint
from runlog.sources.runna.fit import parse_fit

_SAMPLE = Path(__file__).parent / "testdata" / "sample.fit"


def test_parse_fit_reads_full_stream() -> None:
    result = parse_fit(_SAMPLE.read_bytes())
    assert len(result.stream) == 283
    # Cadence is doubled to full steps/min; indoor-start points have no GPS fix.
    assert result.stream[0] == StreamPoint(
        offset_s=0,
        distance_m=0.0,
        lat=None,
        lng=None,
        altitude_m=0.0,
        hr=89.0,
        cadence=186.0,
        velocity_mps=1.422,
        watts=82.0,
    )


def test_parse_fit_builds_laps_with_heart_rate_from_records() -> None:
    result = parse_fit(_SAMPLE.read_bytes())
    # FIT lap HR fields are zero; avg_hr here is recomputed from the records.
    assert result.laps == (
        Lap(
            lap_index=0,
            elapsed_s=724,
            distance_m=2007.65,
            avg_hr=144.0,
            avg_pace_s_per_km=360.3915025029263,
        ),
        Lap(
            lap_index=1,
            elapsed_s=2,
            distance_m=3.09,
            avg_hr=146.0,
            avg_pace_s_per_km=501.61812297734633,
        ),
    )
