"""View model for the report's Activities tab and per-workout detail pages.

Read-only. ``build_rows`` turns the canonical runs into one summary row each for
the sortable table; ``build_detail`` loads a single run's full per-second stream
and reuses :func:`runlog.plan.progress.workout_detail` for its laps, zone split,
and physiology, producing the data a per-workout page renders as inline SVG.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import TYPE_CHECKING

from runlog.analyze import metrics
from runlog.domain import ActivityId
from runlog.plan import progress

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence

    from runlog.analyze.metrics import Run


def activity_page(activity_id: int) -> str:
    return f"activities/{activity_id}.html"


@dataclass(frozen=True)
class ActivityRow:
    """One run as a sortable table row; numeric fields drive client-side sort."""

    activity_id: int
    page: str
    date: str
    weekday: str
    sport: str
    indoor: bool
    session: str  # detected run type: Easy / Long / Threshold / Intervals / ...
    source: str
    distance_km: float
    duration_s: int
    duration_disp: str
    pace_s: float  # 0.0 sorts to the end (missing)
    pace_disp: str
    avg_hr: int | None
    max_hr: int | None
    cadence: int | None
    elevation_m: int | None
    calories: int | None


@dataclass(frozen=True)
class LapRow:
    index: int
    distance_disp: str
    time_disp: str
    pace_disp: str
    avg_hr: int | None
    max_hr: int | None


@dataclass(frozen=True)
class Chart:
    title: str
    unit: str
    color: str
    y_format: str
    points: list[tuple[float, float | None]]


@dataclass(frozen=True)
class ZoneBand:
    """Time spent in one Karvonen HR zone, with its bpm range."""

    label: str  # Z1..Z5
    name: str  # Recovery / Endurance / Tempo / Threshold / VO2max
    bpm_range: str
    seconds: int
    pct: int


@dataclass(frozen=True)
class ActivityDetail:
    activity_id: int
    title: str
    subtitle: str
    session: str
    elevation_m: int | None
    header: list[tuple[str, str]]
    charts: list[Chart]
    zones: list[ZoneBand]
    thresholds: list[tuple[str, str]]
    laps: list[LapRow]


@dataclass(frozen=True)
class _StreamPt:
    offset_s: int
    distance_m: float | None
    hr: float | None
    velocity_mps: float | None
    cadence: float | None
    altitude_m: float | None


def _fmt_duration(seconds: int | None) -> str:
    if not seconds:
        return "-"
    hours, rem = divmod(int(seconds), 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _fmt_pace(pace_s_per_km: float | None) -> str:
    if not pace_s_per_km:
        return "-"
    total = int(round(pace_s_per_km))
    return f"{total // 60}:{total % 60:02d}/km"


def _sport_label(run: Run) -> str:
    return "Treadmill" if run.is_indoor else "Run"


def _twin_ids(conn: sqlite3.Connection, activity_id: ActivityId) -> list[int]:
    """Activities linked to this one as the same physical run (both directions)."""
    queries = (
        ("activity_links", "strava_activity_id", "apple_activity_id"),
        ("activity_links", "apple_activity_id", "strava_activity_id"),
        ("runna_links", "runna_activity_id", "twin_activity_id"),
        ("runna_links", "twin_activity_id", "runna_activity_id"),
    )
    twins: list[int] = []
    for table, key, other in queries:
        twins += [
            int(row[0])
            for row in conn.execute(
                f"SELECT {other} FROM {table} WHERE {key} = ?", (int(activity_id),)
            )
        ]
    return twins


def _stream_source(conn: sqlite3.Connection, activity_id: ActivityId) -> ActivityId:
    """The canonical id, or a linked twin's, that actually carries a stream.

    The canonical Strava row often lacks the per-second trace that its dropped
    Apple twin recorded, so fall back to a twin that has one.
    """
    for candidate in (int(activity_id), *_twin_ids(conn, activity_id)):
        if conn.execute(
            "SELECT 1 FROM stream_points WHERE activity_id = ? LIMIT 1", (candidate,)
        ).fetchone():
            return ActivityId(candidate)
    return activity_id


def _load_stream(conn: sqlite3.Connection, activity_id: ActivityId) -> list[_StreamPt]:
    return [
        _StreamPt(
            offset_s=int(row["offset_s"]),
            distance_m=row["distance_m"],
            hr=row["hr"],
            velocity_mps=row["velocity_mps"],
            cadence=row["cadence"],
            altitude_m=row["altitude_m"],
        )
        for row in conn.execute(
            """
            SELECT offset_s, distance_m, hr, velocity_mps, cadence, altitude_m
            FROM stream_points
            WHERE activity_id = ? AND offset_s IS NOT NULL
            ORDER BY seq
            """,
            (int(activity_id),),
        )
    ]


def _pace_from_velocity(velocity_mps: float | None) -> float | None:
    """Pace in s/km, dropping implausible values (stops, GPS spikes) as gaps."""
    if not velocity_mps or velocity_mps <= 0:
        return None
    pace = 1000.0 / velocity_mps
    low, high = metrics.PLAUSIBLE_PACE_S_PER_KM
    return pace if low <= pace <= high else None


# Physiological name per Karvonen zone, shown next to Z1..Z5.
_ZONE_NAMES = ("Recovery", "Endurance", "Tempo", "Threshold", "VO2max")
# Lactate-threshold estimates as a fraction of heart-rate reserve: LT1 (aerobic
# threshold) at the Z2/Z3 boundary, LT2 (lactate/anaerobic threshold) near the
# top of the threshold band (Z4/Z5 boundary).
_LT1_FRACTION = 0.70
_LT2_FRACTION = 0.90


def _zone_seconds(
    stream: list[_StreamPt], hr_max: float, hr_rest: float
) -> list[float]:
    """Seconds in each Karvonen zone (Z1..Z5), dt-weighted across the stream."""
    buckets = [0.0] * len(_ZONE_NAMES)
    if hr_max <= hr_rest:
        return buckets
    for current, nxt in zip(stream, stream[1:], strict=False):
        if current.hr is None:
            continue
        dt = nxt.offset_s - current.offset_s
        fraction = metrics.hr_reserve_fraction(current.hr, hr_max, hr_rest)
        index = metrics.hr_zone_index(fraction) if fraction is not None else None
        if dt > 0 and index is not None:
            buckets[index] += dt
    return buckets


def _zone_bands(
    zone_secs: list[float], hr_max: float, hr_rest: float
) -> list[ZoneBand]:
    total = sum(zone_secs)
    bounds = metrics.hr_zone_bounds(hr_max, hr_rest)
    bands: list[ZoneBand] = []
    for i, (label, low, high) in enumerate(bounds):
        bands.append(
            ZoneBand(
                label=label,
                name=_ZONE_NAMES[i],
                bpm_range=f"{low}-{high}" if high is not None else f"{low}+",
                seconds=round(zone_secs[i]),
                pct=round(100 * zone_secs[i] / total) if total else 0,
            )
        )
    return bands


def _thresholds(hr_max: float, hr_rest: float) -> list[tuple[str, str]]:
    if hr_max <= hr_rest:
        return []
    reserve = hr_max - hr_rest
    lt1 = round(hr_rest + _LT1_FRACTION * reserve)
    lt2 = round(hr_rest + _LT2_FRACTION * reserve)
    return [
        ("LT1 (aerobic)", f"{lt1} bpm"),
        ("LT2 (lactate threshold)", f"{lt2} bpm"),
    ]


def _detect_session(
    distance_km: float | None, zone_secs: list[float], structured: bool
) -> str:
    """Infer the session type from distance, HR-zone mix, and lap structure."""
    total = sum(zone_secs)
    if total <= 0:
        return "Run"
    z = [s / total for s in zone_secs]  # Z1..Z5 shares
    hard = z[3] + z[4]
    if distance_km is not None and distance_km >= 15:
        return "Long"
    if structured and hard >= 0.12:
        return "Intervals"
    if z[4] >= 0.15:
        return "VO2max"
    if hard >= 0.25:
        return "Threshold"
    if z[2] >= 0.40:
        return "Tempo"
    if z[0] >= 0.65:
        return "Recovery"
    return "Easy"


def _charts(stream: list[_StreamPt]) -> list[Chart]:
    minutes = [pt.offset_s / 60.0 for pt in stream]
    series: list[tuple[str, str, str, str, list[float | None]]] = [
        ("Heart rate", "bpm", "#dc2626", "int", [pt.hr for pt in stream]),
        (
            "Pace",
            "min/km",
            "#2563eb",
            "pace",
            [_pace_from_velocity(pt.velocity_mps) for pt in stream],
        ),
        ("Cadence", "spm", "#7c3aed", "int", [pt.cadence for pt in stream]),
        ("Elevation", "m", "#16a34a", "int", [pt.altitude_m for pt in stream]),
    ]
    charts: list[Chart] = []
    for title, unit, color, y_format, values in series:
        present = [v for v in values if v is not None]
        if len(present) < 2 or (title == "Elevation" and max(present) == min(present)):
            continue
        charts.append(
            Chart(
                title=title,
                unit=unit,
                color=color,
                y_format=y_format,
                points=list(zip(minutes, values, strict=True)),
            )
        )
    return charts


def _lap_rows(laps: Sequence[progress.LapSplit]) -> list[LapRow]:
    return [
        LapRow(
            index=lap.index + 1,
            distance_disp=(f"{lap.distance_m / 1000:.2f}" if lap.distance_m else "-"),
            time_disp=_fmt_duration(lap.seconds),
            pace_disp=_fmt_pace(lap.pace_s_per_km),
            avg_hr=round(lap.avg_hr) if lap.avg_hr else None,
            max_hr=round(lap.max_hr) if lap.max_hr else None,
        )
        for lap in laps
    ]


def _header(detail: progress.WorkoutDetail, run: Run) -> list[tuple[str, str]]:
    chips: list[tuple[str, str]] = [
        ("Distance", f"{run.distance_km:.2f} km" if run.distance_km else "-"),
        ("Duration", _fmt_duration(detail.moving_s)),
        ("Avg pace", _fmt_pace(detail.avg_pace_s_per_km)),
        ("Avg HR", f"{round(detail.avg_hr)} bpm" if detail.avg_hr else "-"),
        ("Max HR", f"{round(detail.max_hr)} bpm" if detail.max_hr else "-"),
        (
            "Avg cadence",
            f"{round(detail.avg_cadence)} spm" if detail.avg_cadence else "-",
        ),
        (
            "Elevation",
            (
                f"{round(detail.elevation_gain_m)} m"
                if detail.elevation_gain_m is not None
                else "-"
            ),
        ),
    ]
    if detail.cardiac_drift_pct is not None:
        chips.append(("Cardiac drift", f"{detail.cardiac_drift_pct:+.1f}%"))
    if detail.gap_pace_s_per_km is not None:
        chips.append(("GAP", _fmt_pace(detail.gap_pace_s_per_km)))
    return chips


def build_detail(
    conn: sqlite3.Connection, run: Run, hr_max: float, hr_rest: float
) -> ActivityDetail:
    """Full-resolution timeline data + laps for one run's detail page.

    Stream and laps come from whichever linked source actually recorded them (the
    canonical Strava row or its Apple twin); summary fields stay from the run.
    """
    source_id = _stream_source(conn, run.activity_id)
    detail_run = (
        run
        if source_id == run.activity_id
        else dataclasses.replace(run, activity_id=source_id)
    )
    detail = progress.workout_detail(conn, detail_run, hr_max, hr_rest)
    stream = _load_stream(conn, source_id)
    zone_secs = _zone_seconds(stream, hr_max, hr_rest)
    session = _detect_session(run.distance_km, zone_secs, len(detail.laps) >= 3)
    date = run.start.date().isoformat()
    return ActivityDetail(
        activity_id=int(run.activity_id),
        title=f"{_sport_label(run)} · {session} · {date}",
        subtitle=f"{run.start.strftime('%A')} · {run.source}",
        session=session,
        elevation_m=(
            round(detail.elevation_gain_m)
            if detail.elevation_gain_m is not None
            else None
        ),
        header=_header(detail, run),
        charts=_charts(stream),
        zones=_zone_bands(zone_secs, hr_max, hr_rest) if sum(zone_secs) else [],
        thresholds=_thresholds(hr_max, hr_rest),
        laps=_lap_rows(detail.laps),
    )


def _row(run: Run, detail: ActivityDetail) -> ActivityRow:
    return ActivityRow(
        activity_id=int(run.activity_id),
        page=activity_page(int(run.activity_id)),
        date=run.start.date().isoformat(),
        weekday=run.start.strftime("%a"),
        sport=_sport_label(run),
        indoor=run.is_indoor,
        session=detail.session,
        source=run.source,
        distance_km=round(run.distance_km or 0.0, 2),
        duration_s=run.moving_s or 0,
        duration_disp=_fmt_duration(run.moving_s),
        pace_s=run.avg_pace_s_per_km or 0.0,
        pace_disp=_fmt_pace(run.avg_pace_s_per_km),
        avg_hr=round(run.avg_hr) if run.avg_hr else None,
        max_hr=round(run.max_hr) if run.max_hr else None,
        cadence=round(run.avg_cadence) if run.avg_cadence else None,
        elevation_m=detail.elevation_m,
        calories=round(run.calories) if run.calories else None,
    )


def build_activities(
    conn: sqlite3.Connection, runs: Sequence[Run], hr_max: float, hr_rest: float
) -> tuple[list[ActivityRow], list[ActivityDetail]]:
    """Table rows (newest first) and the detail payload for each run."""
    details = {
        int(run.activity_id): build_detail(conn, run, hr_max, hr_rest) for run in runs
    }
    rows = [
        _row(run, details[int(run.activity_id)])
        for run in sorted(runs, key=lambda r: r.start, reverse=True)
    ]
    return rows, list(details.values())
