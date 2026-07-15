"""Load -> next-day recovery dose-response.

Quantifies how yesterday's training load moves today's recovery markers:
pair each day's ALL-SPORT Banister TRIMP (strength and walks included — every
recorded sport carries avg HR and duration) with the NEXT day's marker reading
scored as a robust z against its own trailing baseline
(:func:`runlog.analyze.anomaly.robust_z_series`). Days are bucketed by dose —
rest (no load), moderate (at or below the median non-zero load), hard (above
it) — and each marker reports the mean next-day deviation per bucket plus an
overall load-vs-response correlation.

Pure and read-only. This is an explicit cross-analysis of training load
against passive recovery data; neither side leaks into the other's totals.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from runlog.analyze import analytics, anomaly, metrics, stats
from runlog.analyze.anomaly import Direction

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence
    from datetime import date

    from runlog.analyze.anomaly import Reading

# Recovery markers and the tail that signals reduced recovery (interpretation
# only — the response math is direction-agnostic).
RESPONSE_MARKERS: tuple[tuple[str, Direction], ...] = (
    ("hrv_sdnn", Direction.LOW),
    ("resting_hr", Direction.HIGH),
    ("sleep_hours", Direction.LOW),
    ("hr_recovery_1min", Direction.LOW),
)
_BUCKETS = ("rest", "moderate", "hard")
# Minimum paired (load day, next-day reading) count before a marker is scored.
_MIN_PAIRS = 14


@dataclass(frozen=True)
class BucketStat:
    label: str  # "rest" | "moderate" | "hard"
    mean_z: float | None  # mean next-day robust z; None when the bucket is empty
    n: int


@dataclass(frozen=True)
class MarkerResponse:
    metric: str
    direction: Direction
    buckets: tuple[BucketStat, ...]  # always (rest, moderate, hard)
    pearson_r: float | None  # load day N vs marker z day N+1, over all pairs
    n_pairs: int
    # Inference: hard-day vs rest-day next-day z (Welch + Hedges' g), and the
    # load-vs-response correlation with Fisher CI and exact-t p.
    rest_vs_hard: stats.GroupTest | None = None
    load_corr: stats.CorrTest | None = None


def marker_response(
    daily_load: Sequence[tuple[date, float]],
    readings: Sequence[Reading],
    metric: str,
    direction: Direction,
) -> MarkerResponse | None:
    """Score one marker's next-day response to load; None when too few pairs.

    Pairing is driven by the readings: each reading is matched with the
    previous day's load, and days absent from ``daily_load`` count as rest
    (every workout is assumed recorded).
    """
    load_by_day = dict(daily_load)
    pairs = [
        (load_by_day.get(reading.day - timedelta(days=1), 0.0), reading.deviation)
        for reading in readings
    ]
    if len(pairs) < _MIN_PAIRS:
        return None
    nonzero = [load for load, _ in pairs if load > 0]
    threshold = statistics.median(nonzero) if nonzero else 0.0

    def bucket_of(load: float) -> str:
        if load == 0:
            return "rest"
        return "moderate" if load <= threshold else "hard"

    grouped: dict[str, list[float]] = {label: [] for label in _BUCKETS}
    for load, z in pairs:
        grouped[bucket_of(load)].append(z)
    buckets = tuple(
        BucketStat(
            label=label,
            mean_z=round(statistics.mean(zs), 2) if (zs := grouped[label]) else None,
            n=len(grouped[label]),
        )
        for label in _BUCKETS
    )
    r = analytics.pearson([load for load, _ in pairs], [z for _, z in pairs])
    return MarkerResponse(
        metric=metric,
        direction=direction,
        buckets=buckets,
        pearson_r=round(r, 2) if r is not None else None,
        n_pairs=len(pairs),
        rest_vs_hard=stats.group_test(grouped["hard"], grouped["rest"]),
        load_corr=stats.corr_test([load for load, _ in pairs], [z for _, z in pairs]),
    )


def load_response(
    conn: sqlite3.Connection,
    hr_max: float,
    hr_rest: float,
    since: date | None = None,
) -> list[MarkerResponse]:
    """Next-day response of every recovery marker to all-sport training load."""
    activities = metrics.canonical_run_activities(
        conn, metrics.ALL_SPORT_TYPES, since=since, min_distance_km=0.0
    )
    daily_load = analytics.daily_trimp(activities, hr_max, hr_rest)
    responses: list[MarkerResponse] = []
    for metric, direction in RESPONSE_MARKERS:
        daily = metrics.daily_means(metrics.metric_series(conn, metric, since=since))
        readings = anomaly.robust_z_series(daily)
        response = marker_response(daily_load, readings, metric, direction)
        if response is not None:
            responses.append(response)
    return responses
