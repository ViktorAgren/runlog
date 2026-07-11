"""Ingest-time data-quality checks over an activity and its stream.

Pure and domain-only. Flags corrupted records once, at write time, so the
analysis layer can quarantine them centrally instead of every consumer
re-deriving the same guards (the recurring 23 h "workout" / 0:00 best-effort /
49 m junk-run bug class). Flags are stored comma-joined in
``activities.quality_flags``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from runlog.domain import Activity, StreamPoint

# Plausible running pace bounds (s/km), mirroring analyze.metrics; a whole-run
# pace outside 2:30-15:00 /km is a data error, not a run.
_MIN_PACE_S_PER_KM = 150.0
_MAX_PACE_S_PER_KM = 900.0
# The stream's offset span exceeding moving time by this factor means the clock
# ran long past the movement (a paused/never-stopped workout).
_SPAN_FACTOR = 3.0
# A "run" shorter than this is a stray GPS fragment, not a session.
_MIN_DISTANCE_M = 200.0
# Distance covered between two ~1 s samples at/above this is a GPS teleport.
_JUMP_M = 500.0

# Flags that make a run unusable for analytics (vs. merely worth noting).
QUARANTINE_FLAGS = frozenset({"implausible_pace", "time_gap", "tiny_distance"})


def stream_flags(activity: Activity, stream: Sequence[StreamPoint]) -> tuple[str, ...]:
    """Return the data-quality flags for one activity + its stream."""
    flags: list[str] = []
    distance = activity.distance_m
    if distance is not None and distance < _MIN_DISTANCE_M:
        flags.append("tiny_distance")
    if activity.moving_s and distance and distance > 0:
        pace = activity.moving_s / (distance / 1000)
        if not _MIN_PACE_S_PER_KM <= pace <= _MAX_PACE_S_PER_KM:
            flags.append("implausible_pace")
    if stream and activity.moving_s:
        span = stream[-1].offset_s - stream[0].offset_s
        if span > activity.moving_s * _SPAN_FACTOR:
            flags.append("time_gap")
    if _has_distance_jump(stream):
        flags.append("distance_jump")
    return tuple(flags)


def _has_distance_jump(stream: Sequence[StreamPoint]) -> bool:
    prev: tuple[int, float] | None = None
    for point in stream:
        if point.distance_m is None:
            continue
        if prev is not None:
            dt = point.offset_s - prev[0]
            dd = point.distance_m - prev[1]
            if 0 <= dt <= 1 and dd >= _JUMP_M:
                return True
        prev = (point.offset_s, point.distance_m)
    return False
