"""Personal-record timeline and new-record detection.

A chronological pass over the runs that emits a :class:`RecordEvent` whenever
a run sets a new best — all-time or within its calendar year — for a fastest
1k/5k/10k (from streams), longest single run, or biggest ISO training week.
Year events are suppressed when they coincide with an all-time event for the
same kind (no duplicate on a PB day).

Pure and read-only. Effort records use seconds (lower is better); distance and
weekly records use kilometres (higher is better).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from runlog.analyze import analytics, metrics, streams

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence
    from datetime import date

    from runlog.analyze.metrics import Run

KINDS = ("1k", "5k", "10k", "longest_run", "biggest_week")
_EFFORT_DISTANCES: tuple[tuple[str, float], ...] = (
    ("1k", 1000.0),
    ("5k", 5000.0),
    ("10k", 10000.0),
)


@dataclass(frozen=True)
class RecordEvent:
    day: date
    kind: str  # one of KINDS
    scope: str  # "all_time" | "year"
    value: float  # seconds for efforts, km for distance/week
    label: str  # display string, e.g. "5k 24:58"


def _clock(seconds: float) -> str:
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _effort_label(kind: str, seconds: float) -> str:
    return f"{kind} {_clock(seconds)}"


def records_timeline(
    conn: sqlite3.Connection, runs: Sequence[Run]
) -> list[RecordEvent]:
    """Every all-time and yearly best over the run history, date-ordered."""
    events: list[RecordEvent] = []
    best_effort: dict[str, float] = {}  # kind -> all-time best seconds
    best_effort_year: dict[tuple[str, int], float] = {}
    best_dist = 0.0
    best_dist_year: dict[int, float] = {}

    for run in sorted(runs, key=lambda r: r.start):
        day = run.start.date()
        year = day.year
        stream = [
            (s.offset_s, s.distance_m)
            for s in streams.full_stream(conn, run.activity_id)
        ]
        for kind, target in _EFFORT_DISTANCES:
            effort = analytics.best_effort_seconds(stream, target)
            if effort is None:
                continue
            year_key = (kind, year)
            new_all_time = kind not in best_effort or effort < best_effort[kind]
            new_year = (
                year_key not in best_effort_year or effort < best_effort_year[year_key]
            )
            if new_all_time:
                best_effort[kind] = effort
                events.append(
                    RecordEvent(
                        day, kind, "all_time", effort, _effort_label(kind, effort)
                    )
                )
            elif new_year:
                events.append(
                    RecordEvent(day, kind, "year", effort, _effort_label(kind, effort))
                )
            if new_year:
                best_effort_year[year_key] = effort

        km = run.distance_km
        if km is not None:
            new_all_time = km > best_dist
            new_year = km > best_dist_year.get(year, 0.0)
            if new_all_time:
                best_dist = km
                events.append(
                    RecordEvent(
                        day, "longest_run", "all_time", km, f"Longest run {km:.1f} km"
                    )
                )
            elif new_year:
                events.append(
                    RecordEvent(
                        day, "longest_run", "year", km, f"Longest run {km:.1f} km"
                    )
                )
            if new_year:
                best_dist_year[year] = km

    events.extend(_week_records(runs))
    return sorted(events, key=lambda e: (e.day, e.kind))


def _week_records(runs: Sequence[Run]) -> list[RecordEvent]:
    """Biggest-week records dated to each record week's last day."""
    events: list[RecordEvent] = []
    best = 0.0
    best_year: dict[int, float] = {}
    for week in metrics.weekly_volume(runs):
        if week.distance_km <= 0:
            continue
        day = week.week_start + timedelta(days=6)
        year = week.week_start.year
        km = week.distance_km
        new_all_time = km > best
        new_year = km > best_year.get(year, 0.0)
        label = f"Biggest week {km:.1f} km"
        if new_all_time:
            best = km
            events.append(RecordEvent(day, "biggest_week", "all_time", km, label))
        elif new_year:
            events.append(RecordEvent(day, "biggest_week", "year", km, label))
        if new_year:
            best_year[year] = km
    return events


def current_records(
    events: Sequence[RecordEvent], scope: str = "all_time", year: int | None = None
) -> dict[str, RecordEvent]:
    """Standing record per kind (events improve over time, so last wins)."""
    result: dict[str, RecordEvent] = {}
    for event in events:
        if scope == "all_time" and event.scope != "all_time":
            continue
        if scope == "year" and year is not None and event.day.year != year:
            continue
        result[event.kind] = event
    return result


def new_records(events: Sequence[RecordEvent], since: date) -> list[RecordEvent]:
    """Records set on or after ``since`` (the fresh-records hook)."""
    return [event for event in events if event.day >= since]


def in_block_records(
    events: Sequence[RecordEvent], start: date
) -> dict[str, RecordEvent]:
    """Best record per kind set during the block (day >= start)."""
    result: dict[str, RecordEvent] = {}
    for event in events:
        if event.day >= start:
            result[event.kind] = event
    return result
