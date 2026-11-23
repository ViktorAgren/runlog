"""View model for the report's Activities tab and per-workout detail pages.

Read-only. ``build_rows`` turns the canonical runs into one summary row each for
the sortable table; ``build_detail`` loads a single run's full per-second stream
and reuses :func:`runlog.plan.progress.workout_detail` for its laps, zone split,
and physiology, producing the data a per-workout page renders as inline SVG.
"""

from __future__ import annotations

import dataclasses
import statistics
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from runlog.analyze import analytics, anomaly, metrics, records, zones
from runlog.db import quality, queries
from runlog.domain import ActivityId
from runlog.plan import progress

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence
    from datetime import date

    from runlog.analyze.metrics import Run
    from runlog.config import Sex


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
    work_pace_s: float | None  # work-rep pace (WU/CD/recovery excluded), else None
    avg_hr: int | None
    max_hr: int | None
    cadence: int | None
    elevation_m: int | None
    calories: int | None
    load: int | None  # per-run Banister TRIMP (relative effort)
    ef: float | None  # efficiency factor: m/min per bpm (economy)
    gap_s: float | None  # grade-adjusted pace, s/km
    gap_disp: str
    decoupling_pct: float | None  # within-run HR-pace drift
    easy_pct: float | None
    moderate_pct: float | None
    hard_pct: float | None
    is_pr: bool  # set an all-time best on this day
    is_off: bool  # efficiency below its rolling baseline (off day)


@dataclass(frozen=True)
class LapRow:
    index: int
    distance_disp: str
    time_disp: str
    pace_disp: str
    avg_hr: int | None
    max_hr: int | None
    cadence: int | None


@dataclass(frozen=True)
class Chart:
    title: str
    unit: str
    color: str
    y_format: str
    points: list[tuple[float, float | None]]
    min_span: float  # smallest y-axis span, so a steady run stays visibly flat


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
    analysis: list[tuple[str, str]]
    charts: list[Chart]
    zones: list[ZoneBand]
    thresholds: list[tuple[str, str]]
    intervals: progress.IntervalSummary | None
    laps: list[LapRow]
    # Row-summary scalars (also useful on the JSON detail endpoint).
    load: int | None
    ef: float | None
    gap_s: float | None
    decoupling_pct: float | None
    easy_pct: float | None
    moderate_pct: float | None
    hard_pct: float | None
    work_pace_s: float | None = None  # work-rep pace (WU/CD/recovery excluded)


@dataclass(frozen=True)
class _StreamPt:
    offset_s: int
    distance_m: float | None
    hr: float | None
    velocity_mps: float | None
    cadence: float | None
    altitude_m: float | None
    watts: float | None


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


def _load_stream(conn: sqlite3.Connection, activity_id: ActivityId) -> list[_StreamPt]:
    return [
        _StreamPt(
            offset_s=int(row["offset_s"]),
            distance_m=row["distance_m"],
            hr=row["hr"],
            velocity_mps=row["velocity_mps"],
            cadence=row["cadence"],
            altitude_m=row["altitude_m"],
            watts=row["watts"],
        )
        for row in conn.execute(
            """
            SELECT offset_s, distance_m, hr, velocity_mps, cadence, altitude_m, watts
            FROM stream_points
            WHERE activity_id = ? AND offset_s IS NOT NULL
            ORDER BY seq
            """,
            (int(activity_id),),
        )
    ]


# Physiological name per Karvonen zone, shown next to Z1..Z5.
_ZONE_NAMES = tuple(zone.name for zone in zones.ZONES)
# Per-workout threshold *heart rates* as a fraction of heart-rate reserve, pinned
# to the zone boundaries. These are HR anchors in bpm and are distinct from
# cs.LT1_OF_LT2, which relates threshold *speeds*.
_LT1_HR_FRACTION = zones.LT1_RESERVE_FRACTION
_LT2_HR_FRACTION = zones.LT2_RESERVE_FRACTION


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
    lt1 = round(hr_rest + _LT1_HR_FRACTION * reserve)
    lt2 = round(hr_rest + _LT2_HR_FRACTION * reserve)
    return [
        ("LT1 (aerobic)", f"{lt1} bpm"),
        ("LT2 (lactate threshold)", f"{lt2} bpm"),
    ]


def _detect_session(
    distance_km: float | None, zone_secs: list[float], is_intervals: bool
) -> str:
    """Infer the session type from HR-zone mix, structure, then distance.

    Intensity is tested before the long-run distance rule, so a hard session over
    15 km reads as its workout type (Threshold/VO2max) rather than "Long".
    """
    total = sum(zone_secs)
    if total <= 0:
        return "Run"
    z = [s / total for s in zone_secs]  # Z1..Z5 shares
    hard = z[3] + z[4]
    if is_intervals and hard >= 0.12:
        return "Intervals"
    if z[4] >= 0.15:
        return "VO2max"
    if hard >= 0.25:
        return "Threshold"
    if distance_km is not None and distance_km >= 15:
        return "Long"
    if z[2] >= 0.40:
        return "Tempo"
    if z[0] >= 0.65:
        return "Recovery"
    return "Easy"


def _run_ef(run: Run) -> float | None:
    """Efficiency factor: speed (m/min) per heartbeat — a running-economy proxy."""
    if not run.distance_m or not run.moving_s or not run.avg_hr:
        return None
    return round(run.distance_m / (run.moving_s / 60.0) / run.avg_hr, 2)


@dataclass(frozen=True)
class _Bucket:
    """One equal-time slice of the run, with each channel aggregated."""

    t_mid_min: float  # bucket midpoint in minutes (chart x)
    t_end_s: float  # last sample offset in the bucket (for cross-bucket pace)
    hr: float | None
    cadence: float | None
    velocity: float | None
    altitude: float | None
    watts: float | None
    dist_cum: float | None  # last cumulative distance in the bucket


# A raw stream is thousands of jittery ~1 Hz samples; resampling to a few hundred
# equal-time buckets gives a smooth, light line that still resolves intervals (a
# 25 min session buckets to ~4 s, a 2 h run to ~24 s).
_TARGET_BUCKETS = 300


def _median_opt(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return statistics.median(present) if present else None


def _resample(stream: list[_StreamPt], target: int = _TARGET_BUCKETS) -> list[_Bucket]:
    """Aggregate a per-point stream into <= ``target`` equal-time buckets.

    Each channel is cleaned (plausible_*) then median-aggregated per bucket, so the
    plotted line is smooth regardless of sample rate; empty buckets are dropped, so
    real gaps show as breaks between kept buckets.
    """
    if len(stream) < 2:
        return []
    start, end = stream[0].offset_s, stream[-1].offset_s
    span = end - start
    if span <= 0:
        return []
    width = span / target
    groups: dict[int, list[_StreamPt]] = defaultdict(list)
    for pt in stream:
        groups[min(int((pt.offset_s - start) / width), target - 1)].append(pt)
    buckets: list[_Bucket] = []
    for idx in sorted(groups):
        g = groups[idx]
        offs = [pt.offset_s for pt in g]
        dists = [pt.distance_m for pt in g if pt.distance_m is not None]
        buckets.append(
            _Bucket(
                t_mid_min=(start + (idx + 0.5) * width) / 60.0,
                t_end_s=offs[-1],
                hr=_median_opt([quality.plausible_hr(pt.hr) for pt in g]),
                cadence=_median_opt(
                    [quality.plausible_cadence(pt.cadence) for pt in g]
                ),
                velocity=_median_opt(
                    [quality.plausible_speed(pt.velocity_mps) for pt in g]
                ),
                altitude=_median_opt([pt.altitude_m for pt in g]),
                watts=_median_opt(
                    [pt.watts if pt.watts and pt.watts > 0 else None for pt in g]
                ),
                dist_cum=max(dists) if dists else None,
            )
        )
    return buckets


def _pace_series(buckets: list[_Bucket]) -> list[float | None]:
    """Pace (s/km) from distance over time between consecutive buckets.

    Distance/time is far steadier than the raw velocity channel (which is often
    quantized on treadmills or jittery on GPS). The first bucket has no prior, so
    it is None.
    """
    low, high = metrics.PLAUSIBLE_PACE_S_PER_KM
    out: list[float | None] = [None]
    for prev, cur in zip(buckets, buckets[1:], strict=False):
        if prev.dist_cum is None or cur.dist_cum is None:
            out.append(None)
            continue
        dd = cur.dist_cum - prev.dist_cum
        dt = cur.t_end_s - prev.t_end_s
        pace = dt / (dd / 1000.0) if dd > 0 and dt > 0 else None
        out.append(pace if pace is not None and low <= pace <= high else None)
    return out


_SMOOTH_FRAC = 0.015  # rolling-median window as a share of the bucket count


def _smooth_window(n: int) -> int:
    window = max(3, round(n * _SMOOTH_FRAC))
    return window if window % 2 else window + 1


def _smooth(values: list[float | None], window: int) -> list[float | None]:
    """Centered rolling median that spans gaps without filling them.

    The resampled channels still carry per-bucket sensor jitter; a short median
    window exposes the interval structure the raw hash hides, while None buckets
    stay None so real breaks remain visible.
    """
    if window <= 1:
        return values
    half = window // 2
    out: list[float | None] = []
    for i, value in enumerate(values):
        if value is None:
            out.append(None)
            continue
        near = [v for v in values[max(0, i - half) : i + half + 1] if v is not None]
        out.append(statistics.median(near) if near else value)
    return out


def _charts(buckets: list[_Bucket]) -> list[Chart]:
    if len(buckets) < 2:
        return []
    x = [b.t_mid_min for b in buckets]
    cadence = quality.to_spm([b.cadence for b in buckets])
    pace = _pace_series(buckets)
    stride = [
        b.velocity / (c / 60.0) if b.velocity and c else None
        for b, c in zip(buckets, cadence, strict=True)
    ]
    efficiency = [
        b.velocity / b.hr * 100 if b.velocity and b.hr else None for b in buckets
    ]
    # (title, unit, color, y_format, min_span, values)
    series: list[tuple[str, str, str, str, float, list[float | None]]] = [
        ("Pace", "min/km", "#2563eb", "pace", 12.0, pace),
        ("Heart rate", "bpm", "#dc2626", "int", 20.0, [b.hr for b in buckets]),
        ("Efficiency", "m/s per bpm x100", "#0891b2", "float", 0.3, efficiency),
        ("Cadence", "spm", "#7c3aed", "int", 10.0, cadence),
        ("Stride", "m", "#0d9488", "float", 0.1, stride),
        ("Power", "W", "#ea580c", "int", 20.0, [b.watts for b in buckets]),
        ("Elevation", "m", "#16a34a", "int", 20.0, [b.altitude for b in buckets]),
    ]
    window = _smooth_window(len(buckets))
    charts: list[Chart] = []
    for title, unit, color, y_format, min_span, values in series:
        plotted = values if title == "Elevation" else _smooth(values, window)
        present = [v for v in plotted if v is not None]
        if len(present) < 2 or (title == "Elevation" and max(present) == min(present)):
            continue
        charts.append(
            Chart(
                title=title,
                unit=unit,
                color=color,
                y_format=y_format,
                points=list(zip(x, plotted, strict=True)),
                min_span=min_span,
            )
        )
    return charts


def _analysis_chips(
    detail: progress.WorkoutDetail, cadence: list[float | None]
) -> list[tuple[str, str]]:
    """A one-line read of the run from already-computed signals."""
    chips: list[tuple[str, str]] = []
    drift = detail.cardiac_drift_pct
    if drift is not None:
        verdict = "held" if abs(drift) < 5 else "faded" if drift > 0 else "improved"
        chips.append(("Aerobic durability", f"efficiency {verdict} ({drift:+.1f}%)"))
    split = detail.negative_split_pct
    if split is not None:
        verdict = (
            "even"
            if abs(split) < 1
            else "negative split" if split > 0 else "faded late"
        )
        chips.append(("Pacing", f"{verdict} ({split:+.1f}%)"))
    present = [c for c in cadence if c is not None]
    if len(present) >= 3:
        mean = statistics.mean(present)
        cv = statistics.pstdev(present) / mean * 100 if mean else 0.0
        steady = "steady" if cv < 3 else "variable"
        chips.append(("Cadence", f"{steady} ({mean:.0f} spm, CV {cv:.1f}%)"))
    return chips


def _lap_rows(laps: Sequence[progress.LapSplit]) -> list[LapRow]:
    return [
        LapRow(
            index=lap.index + 1,
            distance_disp=(f"{lap.distance_m / 1000:.2f}" if lap.distance_m else "-"),
            time_disp=_fmt_duration(lap.seconds),
            pace_disp=_fmt_pace(lap.pace_s_per_km),
            avg_hr=round(lap.avg_hr) if lap.avg_hr else None,
            max_hr=round(lap.max_hr) if lap.max_hr else None,
            cadence=round(lap.avg_cadence) if lap.avg_cadence else None,
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
    if detail.avg_power_w is not None:
        chips.append(("Avg power", f"{round(detail.avg_power_w)} W"))
    if detail.avg_stride_length_m is not None:
        chips.append(("Avg stride", f"{detail.avg_stride_length_m:.2f} m"))
    if detail.gap_pace_s_per_km is not None:
        chips.append(("GAP", _fmt_pace(detail.gap_pace_s_per_km)))
    return chips


def build_detail(
    conn: sqlite3.Connection,
    run: Run,
    hr_max: float,
    hr_rest: float,
    sex: Sex | None = None,
) -> ActivityDetail:
    """Full-resolution timeline data + laps for one run's detail page.

    Stream and laps come from whichever linked source actually recorded them (the
    canonical Strava row or its Apple twin); summary fields stay from the run.
    """
    source_id = queries.stream_bearing_id(conn, run.activity_id)
    detail_run = (
        run
        if source_id == run.activity_id
        else dataclasses.replace(run, activity_id=source_id)
    )
    detail = progress.workout_detail(conn, detail_run, hr_max, hr_rest)
    stream = _load_stream(conn, source_id)
    buckets = _resample(stream)
    zone_secs = _zone_seconds(stream, hr_max, hr_rest)
    intervals = progress.interval_summary(detail.laps)
    session = _detect_session(run.distance_km, zone_secs, intervals is not None)
    load = analytics.run_trimp(run, hr_max, hr_rest, sex)
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
        analysis=_analysis_chips(detail, quality.to_spm([b.cadence for b in buckets])),
        charts=_charts(buckets),
        zones=_zone_bands(zone_secs, hr_max, hr_rest) if sum(zone_secs) else [],
        thresholds=_thresholds(hr_max, hr_rest),
        intervals=intervals,
        laps=_lap_rows(detail.laps),
        load=round(load) if load else None,
        ef=_run_ef(run),
        gap_s=detail.gap_pace_s_per_km,
        decoupling_pct=detail.cardiac_drift_pct,
        easy_pct=detail.easy_pct,
        moderate_pct=detail.moderate_pct,
        hard_pct=detail.hard_pct,
        work_pace_s=progress.work_pace_s_per_km(detail.laps),
    )


def _row(
    run: Run, detail: ActivityDetail, pr_days: set[date], off_days: set[date]
) -> ActivityRow:
    day = run.start.date()
    return ActivityRow(
        activity_id=int(run.activity_id),
        page=activity_page(int(run.activity_id)),
        date=day.isoformat(),
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
        work_pace_s=detail.work_pace_s,
        avg_hr=round(run.avg_hr) if run.avg_hr else None,
        max_hr=round(run.max_hr) if run.max_hr else None,
        cadence=round(run.avg_cadence) if run.avg_cadence else None,
        elevation_m=detail.elevation_m,
        calories=round(run.calories) if run.calories else None,
        load=detail.load,
        ef=detail.ef,
        gap_s=detail.gap_s,
        gap_disp=_fmt_pace(detail.gap_s),
        decoupling_pct=detail.decoupling_pct,
        easy_pct=detail.easy_pct,
        moderate_pct=detail.moderate_pct,
        hard_pct=detail.hard_pct,
        is_pr=day in pr_days,
        is_off=day in off_days,
    )


def build_activities(
    conn: sqlite3.Connection,
    runs: Sequence[Run],
    hr_max: float,
    hr_rest: float,
    sex: Sex | None = None,
) -> tuple[list[ActivityRow], list[ActivityDetail]]:
    """Table rows (newest first) and the detail payload for each run."""
    details = {
        int(run.activity_id): build_detail(conn, run, hr_max, hr_rest, sex)
        for run in runs
    }
    pr_days = {
        e.day for e in records.records_timeline(conn, runs) if e.scope == "all_time"
    }
    off_days = {a.day for a in anomaly.performance_anomalies(runs)}
    rows = [
        _row(run, details[int(run.activity_id)], pr_days, off_days)
        for run in sorted(runs, key=lambda r: r.start, reverse=True)
    ]
    return rows, list(details.values())
