"""Unit tests for ingest-time data-quality flags."""

from __future__ import annotations

from datetime import UTC, datetime

from runlog.db import quality
from runlog.domain import Activity, SourceId, StreamPoint


def _activity(distance_m: float | None, moving_s: int | None) -> Activity:
    return Activity(
        source="strava",
        source_id=SourceId("s:1"),
        sport_type="Run",
        start_time_utc=datetime(2026, 6, 1, tzinfo=UTC),
        distance_m=distance_m,
        moving_s=moving_s,
    )


def _stream(*points: tuple[int, float]) -> tuple[StreamPoint, ...]:
    return tuple(StreamPoint(offset_s=o, distance_m=d) for o, d in points)


def test_clean_run_has_no_flags() -> None:
    stream = _stream(*[(i, float(i) * 3.0) for i in range(0, 400)])
    assert quality.stream_flags(_activity(5000.0, 1500), stream) == ()


def test_flags_the_corrupted_stream_bug_classes() -> None:
    # A paused workout (23 h span over a 34 min run) and a GPS teleport.
    corrupt = _stream((0, 0.0), (1, 1000.0), (82946, 5000.0))
    assert set(quality.stream_flags(_activity(5000.0, 2074), corrupt)) == {
        "time_gap",
        "distance_jump",
    }


def test_implausible_pace_and_tiny_distance() -> None:
    # 5 km in 60 s -> ~12 s/km, and a 49 m fragment.
    assert (
        quality.stream_flags(_activity(5000.0, 60), ()),
        quality.stream_flags(_activity(49.0, 30), ()),
    ) == (("implausible_pace",), ("tiny_distance",))
