"""Core domain types: branded identifiers and immutable records.

These types are source-agnostic. Both the Strava and Apple Health ingest paths
normalize their payloads into these structures before anything touches the
database, so the storage layer never sees source-specific shapes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, NewType

# Internal auto-increment primary key assigned by SQLite.
ActivityId = NewType("ActivityId", int)
# The identifier a source uses for an activity (Strava activity id, Apple UUID,
# or a synthesized key for bulk rows). Unique only within a source.
SourceId = NewType("SourceId", str)

Source = Literal["strava", "apple_health"]


@dataclass(frozen=True)
class Activity:
    """One run/workout as reported by a single source.

    Optional fields are ``None`` when the source does not provide them; the
    bulk CSV, for instance, lacks a moving time for older activities.
    """

    source: Source
    source_id: SourceId
    sport_type: str
    start_time_utc: datetime
    tz: str | None = None
    elapsed_s: int | None = None
    moving_s: int | None = None
    distance_m: float | None = None
    avg_hr: float | None = None
    max_hr: float | None = None
    avg_pace_s_per_km: float | None = None
    avg_cadence: float | None = None
    elevation_gain_m: float | None = None
    calories: float | None = None
    name: str | None = None
    raw_path: str | None = None
    # Strava extras (from the bulk CSV).
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
    # Apple running dynamics (from workout statistics).
    avg_power_w: float | None = None
    avg_stride_length_m: float | None = None
    avg_vertical_oscillation_cm: float | None = None
    avg_ground_contact_ms: float | None = None
    avg_running_speed_mps: float | None = None


@dataclass(frozen=True)
class Lap:
    """A single lap/split within an activity (e.g. an interval repeat)."""

    lap_index: int
    elapsed_s: int | None = None
    distance_m: float | None = None
    avg_hr: float | None = None
    avg_pace_s_per_km: float | None = None


@dataclass(frozen=True)
class StreamPoint:
    """One sample of the per-point time series for an activity.

    ``offset_s`` is seconds from the activity start; it is the ordering key.
    """

    offset_s: int
    distance_m: float | None = None
    lat: float | None = None
    lng: float | None = None
    altitude_m: float | None = None
    hr: float | None = None
    cadence: float | None = None
    velocity_mps: float | None = None
    watts: float | None = None


@dataclass(frozen=True)
class ActivityRecord:
    """An activity together with its laps and per-point stream.

    This is the unit an ingest produces and the store persists atomically.
    """

    activity: Activity
    laps: tuple[Lap, ...] = field(default_factory=tuple)
    stream: tuple[StreamPoint, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class HealthMetric:
    """A non-workout Apple Health measurement (resting HR, HRV, VO2max, ...)."""

    metric_type: str
    start_time_utc: datetime
    value: float
    unit: str | None = None
    end_time_utc: datetime | None = None
    source: str | None = None


@dataclass(frozen=True)
class RawFile:
    """Manifest entry for a payload archived verbatim in the raw landing zone."""

    path: str
    source: Source
    source_id: SourceId
    fetched_at: datetime
    sha256: str
