"""Pure aggregations over the runlog database for trend analysis.

Everything here is read-only and free of matplotlib: functions take a SQLite
connection (or already-loaded runs) and return plain dataclasses/lists, so they
are unit-testable against a small in-memory database.

The same outdoor run can live in both Strava and Apple Health. To avoid
double-counting, all run-level metrics build on :func:`canonical_run_activities`,
which keeps the Strava row of each linked pair (richer HR/GPS streams) and drops
the Apple duplicate.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from runlog.domain import ActivityId, Source

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Sequence

_RUN_SPORTS = ("Run", "Running")
_ROLLING_WEEKS = 4
_DISTANCE_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("<3k", 0.0, 3.0),
    ("3-5k", 3.0, 5.0),
    ("5-10k", 5.0, 10.0),
    ("10k+", 10.0, float("inf")),
)
FITNESS_METRICS = ("vo2max", "resting_hr", "hrv_sdnn")

# Sanity bounds that filter data-entry / GPS / sensor artifacts.
MIN_DISTANCE_KM = 1.0
# Plausible running pace: 2:30/km (elite) to 15:00/km (very easy). Values
# outside this are almost always a tiny-distance or GPS glitch.
PLAUSIBLE_PACE_S_PER_KM = (150.0, 900.0)
_METRIC_RANGES: dict[str, tuple[float, float]] = {
    "vo2max": (20.0, 90.0),
    "resting_hr": (25.0, 120.0),
    "hrv_sdnn": (0.0, 200.0),
    "spo2": (0.5, 1.0),
    "hr_recovery_1min": (0.0, 100.0),
    "body_mass": (30.0, 200.0),
    "walking_speed": (0.0, 3.0),
    "walking_asymmetry": (0.0, 1.0),
    "walking_steadiness": (0.0, 1.0),
    "physical_effort": (0.0, 20.0),
    "sleep_hours": (0.0, 16.0),
    "active_energy": (0.0, 10000.0),
    "exercise_minutes": (0.0, 600.0),
    "steps": (0.0, 100000.0),
    "flights_climbed": (0.0, 500.0),
}


def _plausible_pace(pace: float) -> bool:
    low, high = PLAUSIBLE_PACE_S_PER_KM
    return low <= pace <= high


@dataclass(frozen=True)
class Run:
    """One canonical run (post de-duplication)."""

    activity_id: ActivityId
    source: Source
    start: datetime
    distance_m: float | None
    moving_s: int | None
    avg_pace_s_per_km: float | None
    avg_hr: float | None
    max_hr: float | None
    tz: str | None = None
    avg_cadence: float | None = None
    elevation_gain_m: float | None = None
    relative_effort: float | None = None
    grade_adj_distance_m: float | None = None
    avg_power_w: float | None = None
    avg_stride_length_m: float | None = None
    avg_vertical_oscillation_cm: float | None = None
    avg_ground_contact_ms: float | None = None

    @property
    def distance_km(self) -> float | None:
        return self.distance_m / 1000 if self.distance_m is not None else None

    @property
    def local_hour(self) -> int:
        """Hour of day in the run's local timezone (falls back to UTC)."""
        if self.tz:
            try:
                return self.start.astimezone(ZoneInfo(self.tz)).hour
            except ZoneInfoNotFoundError:
                return self.start.hour
        return self.start.hour


def canonical_run_activities(
    conn: sqlite3.Connection,
    sports: Sequence[str] = _RUN_SPORTS,
    since: date | None = None,
    min_distance_km: float = MIN_DISTANCE_KM,
) -> list[Run]:
    """Return one :class:`Run` per real run.

    De-duplicates by dropping the Apple twin of each link, discards junk
    activities shorter than ``min_distance_km`` (accidental/aborted starts), and
    optionally limits to runs on/after ``since``.

    The kept Strava row inherits the running-dynamics fields (power, stride,
    vertical oscillation, ground contact) from its dropped Apple twin, since
    those live only on the Apple side.
    """
    twin_of = {
        int(row["strava_activity_id"]): int(row["apple_activity_id"])
        for row in conn.execute(
            "SELECT strava_activity_id, apple_activity_id FROM activity_links"
        )
    }
    linked_apple = set(twin_of.values())
    apple_dynamics = _apple_dynamics(conn, linked_apple)
    placeholders = ",".join("?" for _ in sports)
    conditions = [f"sport_type IN ({placeholders})"]
    params: list[object] = list(sports)
    if since is not None:
        conditions.append("start_time_utc >= ?")
        params.append(since.isoformat())
    where = " AND ".join(conditions)
    runs: list[Run] = []
    for row in conn.execute(
        f"""
        SELECT id, source, start_time_utc, tz, distance_m, moving_s,
               avg_pace_s_per_km, avg_hr, max_hr, avg_cadence, elevation_gain_m,
               relative_effort, grade_adj_distance_m, avg_power_w,
               avg_stride_length_m, avg_vertical_oscillation_cm,
               avg_ground_contact_ms
        FROM activities
        WHERE {where}
        ORDER BY start_time_utc
        """,
        tuple(params),
    ):
        if int(row["id"]) in linked_apple:
            continue
        distance_m = row["distance_m"]
        if distance_m is not None and distance_m / 1000 < min_distance_km:
            continue
        twin = apple_dynamics.get(twin_of.get(int(row["id"]), -1), {})
        runs.append(
            Run(
                activity_id=ActivityId(int(row["id"])),
                source=row["source"],
                start=datetime.fromisoformat(row["start_time_utc"]),
                tz=row["tz"],
                distance_m=row["distance_m"],
                moving_s=row["moving_s"],
                avg_pace_s_per_km=row["avg_pace_s_per_km"],
                avg_hr=row["avg_hr"],
                max_hr=row["max_hr"],
                avg_cadence=row["avg_cadence"],
                elevation_gain_m=row["elevation_gain_m"],
                relative_effort=row["relative_effort"],
                grade_adj_distance_m=row["grade_adj_distance_m"],
                avg_power_w=_coalesce(row, twin, "avg_power_w"),
                avg_stride_length_m=_coalesce(row, twin, "avg_stride_length_m"),
                avg_vertical_oscillation_cm=_coalesce(
                    row, twin, "avg_vertical_oscillation_cm"
                ),
                avg_ground_contact_ms=_coalesce(row, twin, "avg_ground_contact_ms"),
            )
        )
    return runs


def _coalesce(row: sqlite3.Row, twin: dict[str, float], field: str) -> float | None:
    """Row's own value for ``field``, else the linked Apple twin's."""
    value: float | None = row[field]
    return value if value is not None else twin.get(field)


# Running-dynamics fields that exist only on the Apple side of a linked pair.
_APPLE_DYNAMICS = (
    "avg_power_w",
    "avg_stride_length_m",
    "avg_vertical_oscillation_cm",
    "avg_ground_contact_ms",
)


def _apple_dynamics(
    conn: sqlite3.Connection, apple_ids: set[int]
) -> dict[int, dict[str, float]]:
    """Map each linked Apple activity id to its non-null running-dynamics values."""
    if not apple_ids:
        return {}
    placeholders = ",".join("?" for _ in apple_ids)
    fields = ", ".join(_APPLE_DYNAMICS)
    return {
        int(row["id"]): {
            field: row[field] for field in _APPLE_DYNAMICS if row[field] is not None
        }
        for row in conn.execute(
            f"SELECT id, {fields} FROM activities WHERE id IN ({placeholders})",
            tuple(apple_ids),
        )
    }


def _week_start(moment: datetime) -> date:
    day = moment.date()
    return day - timedelta(days=day.weekday())


# --- Volume & consistency ---------------------------------------------------


@dataclass(frozen=True)
class WeeklyVolume:
    week_start: date
    distance_km: float
    run_count: int
    rolling_km: float


def weekly_volume(runs: Sequence[Run]) -> list[WeeklyVolume]:
    """Distance/run-count per ISO week (Monday), gap-filled, with rolling mean.

    Weeks with no runs are included as zero so the rolling mean and streak logic
    see a continuous timeline.
    """
    dated = [r for r in runs if r.distance_km is not None]
    if not dated:
        return []
    km_by_week: dict[date, float] = defaultdict(float)
    count_by_week: dict[date, int] = defaultdict(int)
    for run in dated:
        week = _week_start(run.start)
        km_by_week[week] += run.distance_km or 0.0
        count_by_week[week] += 1

    first, last = min(km_by_week), max(km_by_week)
    weeks: list[date] = []
    cursor = first
    while cursor <= last:
        weeks.append(cursor)
        cursor += timedelta(weeks=1)

    result: list[WeeklyVolume] = []
    for index, week in enumerate(weeks):
        window = [
            km_by_week.get(w, 0.0)
            for w in weeks[max(0, index - _ROLLING_WEEKS + 1) : index + 1]
        ]
        result.append(
            WeeklyVolume(
                week_start=week,
                distance_km=round(km_by_week.get(week, 0.0), 2),
                run_count=count_by_week.get(week, 0),
                rolling_km=round(sum(window) / len(window), 2),
            )
        )
    return result


def monthly_distance_by_year(runs: Sequence[Run]) -> dict[int, list[float]]:
    """Map year -> 12 monthly distances (km), index 0 = January."""
    by_year: dict[int, list[float]] = {}
    for run in runs:
        if run.distance_km is None:
            continue
        months = by_year.setdefault(run.start.year, [0.0] * 12)
        months[run.start.month - 1] += run.distance_km
    return {year: [round(v, 2) for v in months] for year, months in by_year.items()}


def distance_distribution(runs: Sequence[Run]) -> list[float]:
    """Per-run distances in km (for a histogram)."""
    return [r.distance_km for r in runs if r.distance_km is not None]


@dataclass(frozen=True)
class Heatmap:
    week_starts: list[date]
    # matrix[weekday][week_index] = km; weekday 0 = Monday.
    matrix: list[list[float]]


def training_heatmap(runs: Sequence[Run]) -> Heatmap:
    """Distance per (weekday, ISO week) for a calendar-style heatmap."""
    dated = [r for r in runs if r.distance_km is not None]
    if not dated:
        return Heatmap(week_starts=[], matrix=[[] for _ in range(7)])
    first = _week_start(min(r.start for r in dated))
    last = _week_start(max(r.start for r in dated))
    weeks: list[date] = []
    cursor = first
    while cursor <= last:
        weeks.append(cursor)
        cursor += timedelta(weeks=1)
    index_of = {week: i for i, week in enumerate(weeks)}
    matrix = [[0.0] * len(weeks) for _ in range(7)]
    for run in dated:
        col = index_of[_week_start(run.start)]
        matrix[run.start.weekday()][col] += run.distance_km or 0.0
    return Heatmap(week_starts=weeks, matrix=matrix)


def active_week_streak(weekly: Sequence[WeeklyVolume]) -> tuple[int, int]:
    """Return (current, longest) run of consecutive weeks with any distance."""
    longest = current = running = 0
    for week in weekly:
        if week.distance_km > 0:
            running += 1
            longest = max(longest, running)
        else:
            running = 0
    current = running
    return current, longest


# --- Pace -------------------------------------------------------------------


@dataclass(frozen=True)
class PacePoint:
    start: datetime
    pace_s_per_km: float
    distance_km: float
    avg_hr: float | None


def pace_points(runs: Sequence[Run]) -> list[PacePoint]:
    """Runs that carry both a pace and a distance."""
    return [
        PacePoint(
            start=r.start,
            pace_s_per_km=r.avg_pace_s_per_km,
            distance_km=r.distance_km,
            avg_hr=r.avg_hr,
        )
        for r in runs
        if r.avg_pace_s_per_km is not None
        and r.distance_km is not None
        and _plausible_pace(r.avg_pace_s_per_km)
    ]


@dataclass(frozen=True)
class BucketPace:
    label: str
    fastest_pace_s_per_km: float | None
    count: int


def fastest_by_bucket(runs: Sequence[Run]) -> list[BucketPace]:
    """Fastest average pace within each distance bucket."""
    result: list[BucketPace] = []
    for label, low, high in _DISTANCE_BUCKETS:
        paces = [
            r.avg_pace_s_per_km
            for r in runs
            if r.avg_pace_s_per_km is not None
            and r.distance_km is not None
            and low <= r.distance_km < high
            and _plausible_pace(r.avg_pace_s_per_km)
        ]
        result.append(
            BucketPace(
                label=label,
                fastest_pace_s_per_km=min(paces) if paces else None,
                count=len(paces),
            )
        )
    return result


def pace_by_weekday(runs: Sequence[Run]) -> list[list[float]]:
    """Seven lists (Mon..Sun) of paces, for a per-weekday box plot."""
    buckets: list[list[float]] = [[] for _ in range(7)]
    for run in runs:
        if run.avg_pace_s_per_km is not None and _plausible_pace(run.avg_pace_s_per_km):
            buckets[run.start.weekday()].append(run.avg_pace_s_per_km)
    return buckets


# --- Enriched per-run trends (form dynamics, effort, grade) ------------------


def run_trend(
    runs: Sequence[Run], value: Callable[[Run], float | None]
) -> list[tuple[date, float]]:
    """One ``(date, value)`` point per run that carries ``value``.

    Generic accessor for the enriched per-run fields (running power, stride
    length, vertical oscillation, ground-contact time, relative effort) so each
    plots through the same daily-marker pipeline as the health metrics.
    """
    points: list[tuple[date, float]] = []
    for run in runs:
        reading = value(run)
        if reading is not None:
            points.append((run.start.date(), reading))
    return points


def grade_adjusted_pace_points(runs: Sequence[Run]) -> list[PacePoint]:
    """Grade-adjusted pace per run (Strava's grade-adjusted distance / time).

    Reuses :class:`PacePoint` (``distance_km`` is the grade-adjusted distance)
    so the standard pace-over-time chart renders it unchanged.
    """
    points: list[PacePoint] = []
    for run in runs:
        gad_m = run.grade_adj_distance_m
        if run.moving_s is None or gad_m is None or gad_m <= 0:
            continue
        pace = run.moving_s / (gad_m / 1000)
        if not _plausible_pace(pace):
            continue
        points.append(
            PacePoint(
                start=run.start,
                pace_s_per_km=pace,
                distance_km=gad_m / 1000,
                avg_hr=run.avg_hr,
            )
        )
    return points


# --- Heart rate & effort ----------------------------------------------------


@dataclass(frozen=True)
class HrPoint:
    start: datetime
    avg_hr: float
    max_hr: float | None


def hr_over_time(runs: Sequence[Run]) -> list[HrPoint]:
    return [
        HrPoint(start=r.start, avg_hr=r.avg_hr, max_hr=r.max_hr)
        for r in runs
        if r.avg_hr is not None
    ]


def hr_samples(conn: sqlite3.Connection, run_ids: Sequence[ActivityId]) -> list[float]:
    """All per-point heart-rate samples for the given runs (for a histogram)."""
    if not run_ids:
        return []
    placeholders = ",".join("?" for _ in run_ids)
    return [
        float(row["hr"])
        for row in conn.execute(
            f"""
            SELECT hr FROM stream_points
            WHERE hr IS NOT NULL AND activity_id IN ({placeholders})
            """,
            tuple(int(i) for i in run_ids),
        )
    ]


@dataclass(frozen=True)
class DriftPoint:
    start: datetime
    distance_km: float
    first_half_hr: float
    second_half_hr: float

    @property
    def drift_pct(self) -> float:
        if self.first_half_hr <= 0:
            return 0.0
        return (self.second_half_hr - self.first_half_hr) / self.first_half_hr * 100


def hr_drift(
    conn: sqlite3.Connection, runs: Sequence[Run], top_n: int = 5
) -> list[DriftPoint]:
    """First-half vs second-half average HR for the longest recent runs."""
    candidates = sorted(
        (r for r in runs if r.distance_km is not None),
        key=lambda r: (r.distance_km or 0.0),
        reverse=True,
    )[:top_n]
    points: list[DriftPoint] = []
    for run in candidates:
        hrs = [
            float(row["hr"])
            for row in conn.execute(
                """
                SELECT hr FROM stream_points
                WHERE activity_id = ? AND hr IS NOT NULL
                ORDER BY seq
                """,
                (int(run.activity_id),),
            )
        ]
        if len(hrs) < 4:
            continue
        mid = len(hrs) // 2
        points.append(
            DriftPoint(
                start=run.start,
                distance_km=run.distance_km or 0.0,
                first_half_hr=round(statistics.mean(hrs[:mid]), 1),
                second_half_hr=round(statistics.mean(hrs[mid:]), 1),
            )
        )
    return sorted(points, key=lambda p: p.start)


# --- Fitness markers --------------------------------------------------------


def metric_series(
    conn: sqlite3.Connection, metric_type: str, since: date | None = None
) -> list[tuple[datetime, float]]:
    """Time series for a health metric, with implausible values dropped.

    Physiological outliers (e.g. an HRV SDNN reading of 340 ms) are excluded via
    :data:`_METRIC_RANGES`; ``since`` limits the series to on/after that date.
    """
    conditions = ["metric_type = ?"]
    params: list[object] = [metric_type]
    if since is not None:
        conditions.append("start_time_utc >= ?")
        params.append(since.isoformat())
    low_high = _METRIC_RANGES.get(metric_type)
    series: list[tuple[datetime, float]] = []
    for row in conn.execute(
        f"""
        SELECT start_time_utc, value FROM health_metrics
        WHERE {" AND ".join(conditions)} ORDER BY start_time_utc
        """,
        tuple(params),
    ):
        value = float(row["value"])
        if low_high is not None and not low_high[0] <= value <= low_high[1]:
            continue
        series.append((datetime.fromisoformat(row["start_time_utc"]), value))
    return series


def daily_means(
    series: Sequence[tuple[datetime, float]],
) -> list[tuple[date, float]]:
    """Collapse an intraday metric series to one mean value per day.

    Health metrics (HRV, resting HR) are sampled many times a day; plotting the
    daily mean instead of every raw reading is what makes the trend legible.
    """
    by_day: dict[date, list[float]] = defaultdict(list)
    for when, value in series:
        by_day[when.date()].append(value)
    return [(day, statistics.mean(values)) for day, values in sorted(by_day.items())]


# --- Overall summary --------------------------------------------------------


@dataclass(frozen=True)
class Summary:
    run_count: int
    total_km: float
    first_run: date | None
    last_run: date | None
    longest_km: float
    this_week_km: float
    this_month_km: float


def overall_summary(runs: Sequence[Run], today: date | None = None) -> Summary:
    today = today or date.today()
    dated = [r for r in runs if r.distance_km is not None]
    distances = [r.distance_km or 0.0 for r in dated]
    this_week = _week_start(datetime(today.year, today.month, today.day))
    week_km = sum(
        r.distance_km or 0.0 for r in dated if _week_start(r.start) == this_week
    )
    month_km = sum(
        r.distance_km or 0.0
        for r in dated
        if r.start.year == today.year and r.start.month == today.month
    )
    return Summary(
        run_count=len(runs),
        total_km=round(sum(distances), 1),
        first_run=min((r.start.date() for r in dated), default=None),
        last_run=max((r.start.date() for r in dated), default=None),
        longest_km=round(max(distances, default=0.0), 2),
        this_week_km=round(week_km, 1),
        this_month_km=round(month_km, 1),
    )


# --- Records & race prediction ----------------------------------------------

# Riegel's endurance exponent for predicting a time at a new distance.
RIEGEL_EXPONENT = 1.06
_RACE_TARGETS: tuple[tuple[str, float], ...] = (
    ("5k", 5.0),
    ("10k", 10.0),
    ("Half", 21.0975),
    ("Marathon", 42.195),
)


@dataclass(frozen=True)
class BestEffort:
    label: str
    pace_s_per_km: float
    when: date


def best_efforts(runs: Sequence[Run]) -> list[BestEffort]:
    """Fastest plausible average pace per distance bucket, with its date."""
    result: list[BestEffort] = []
    for label, low, high in _DISTANCE_BUCKETS:
        candidates = [
            (r.avg_pace_s_per_km, r.start.date())
            for r in runs
            if r.avg_pace_s_per_km is not None
            and r.distance_km is not None
            and low <= r.distance_km < high
            and _plausible_pace(r.avg_pace_s_per_km)
        ]
        if candidates:
            pace, when = min(candidates)
            result.append(BestEffort(label=label, pace_s_per_km=pace, when=when))
    return result


@dataclass(frozen=True)
class RacePrediction:
    label: str
    distance_km: float
    seconds: float


def predict_races(
    runs: Sequence[Run], reference_min_km: float = 3.0
) -> list[RacePrediction]:
    """Predict race times via Riegel from the fastest qualifying run.

    ``T2 = T1 * (D2 / D1) ** 1.06`` using the fastest run of at least
    ``reference_min_km`` as the reference. Empty if no such run exists.
    """
    refs = [
        r
        for r in runs
        if r.distance_km is not None
        and r.moving_s
        and r.distance_km >= reference_min_km
        and r.avg_pace_s_per_km is not None
        and _plausible_pace(r.avg_pace_s_per_km)
    ]
    if not refs:
        return []
    ref = min(refs, key=lambda r: r.avg_pace_s_per_km or float("inf"))
    ref_km = ref.distance_km or 0.0
    ref_s = float(ref.moving_s or 0)
    return [
        RacePrediction(
            label=label,
            distance_km=dist,
            seconds=ref_s * (dist / ref_km) ** RIEGEL_EXPONENT,
        )
        for label, dist in _RACE_TARGETS
    ]


# --- Consistency & rest -----------------------------------------------------


@dataclass(frozen=True)
class ConsistencySummary:
    runs_per_week: float
    active_days: int
    span_days: int
    longest_layoff_days: int
    median_gap_days: float


def run_gap_days(runs: Sequence[Run]) -> list[int]:
    """Days between consecutive run-days (0 for two runs on the same day)."""
    days = sorted({r.start.date() for r in runs})
    return [
        (later - earlier).days for earlier, later in zip(days, days[1:], strict=False)
    ]


def consistency_summary(runs: Sequence[Run]) -> ConsistencySummary:
    """How regularly the athlete trains: cadence, active days, layoffs."""
    days = sorted({r.start.date() for r in runs})
    if not days:
        return ConsistencySummary(0.0, 0, 0, 0, 0.0)
    span = (days[-1] - days[0]).days + 1
    gaps = run_gap_days(runs)
    return ConsistencySummary(
        runs_per_week=round(len(runs) / (span / 7), 2),
        active_days=len(days),
        span_days=span,
        longest_layoff_days=max(gaps, default=0),
        median_gap_days=round(statistics.median(gaps), 1) if gaps else 0.0,
    )


# --- Heart-rate zones & training load (workout HR only) ---------------------

# Zone boundaries as a fraction of estimated HR max. Z5 upper bound is open.
_HR_ZONES: tuple[tuple[str, float, float], ...] = (
    ("Z1", 0.50, 0.60),
    ("Z2", 0.60, 0.70),
    ("Z3", 0.70, 0.80),
    ("Z4", 0.80, 0.90),
    ("Z5", 0.90, 10.0),
)


def estimated_hr_max(samples: Sequence[float]) -> float:
    """Estimate HR max from the highest workout sample (fallback 190)."""
    return max(samples) if samples else 190.0


@dataclass(frozen=True)
class HrZone:
    label: str
    seconds: int
    low_bpm: int
    high_bpm: int | None  # None means open-ended (Z5)

    @property
    def minutes(self) -> float:
        return round(self.seconds / 60, 1)

    @property
    def bpm_range(self) -> str:
        return (
            f"{self.low_bpm}-{self.high_bpm}" if self.high_bpm else f"{self.low_bpm}+"
        )


def hr_zone_seconds(samples: Sequence[float], hr_max: float) -> list[HrZone]:
    """Time (approx, ~1 sample/s) in each HR zone from workout HR samples.

    Zones are percentages of ``hr_max``; each carries its bpm range so figures
    can show exactly which heart-rate band each zone covers.
    """
    counts = [0] * len(_HR_ZONES)
    for hr in samples:
        fraction = hr / hr_max if hr_max else 0.0
        for index, (_label, low, high) in enumerate(_HR_ZONES):
            if low <= fraction < high:
                counts[index] += 1
                break
    zones: list[HrZone] = []
    for index, (label, low, high) in enumerate(_HR_ZONES):
        zones.append(
            HrZone(
                label=label,
                seconds=counts[index],
                low_bpm=round(low * hr_max),
                high_bpm=None if high >= 10 else round(high * hr_max),
            )
        )
    return zones


@dataclass(frozen=True)
class WeeklyLoad:
    week_start: date
    load: float


def weekly_training_load(runs: Sequence[Run], hr_max: float) -> list[WeeklyLoad]:
    """Weekly HR-weighted load: sum of moving-minutes x (avg_hr / hr_max)."""
    by_week: dict[date, float] = defaultdict(float)
    for run in runs:
        if run.avg_hr is not None and run.moving_s and hr_max:
            by_week[_week_start(run.start)] += (run.moving_s / 60) * (
                run.avg_hr / hr_max
            )
    return [WeeklyLoad(week, round(load, 1)) for week, load in sorted(by_week.items())]


# --- Cadence, elevation & timing --------------------------------------------


def cadence_points(runs: Sequence[Run]) -> list[tuple[datetime, float]]:
    """Average cadence (steps/min) per run over time.

    Derived at ingest from total steps / duration, so it is consistent full
    cadence across Strava (Total Steps) and Apple (StepCount).
    """
    return [(r.start, r.avg_cadence) for r in runs if r.avg_cadence is not None]


def monthly_elevation_by_year(runs: Sequence[Run]) -> dict[int, list[float]]:
    """Map year -> 12 monthly elevation-gain totals (m), index 0 = January."""
    by_year: dict[int, list[float]] = {}
    for run in runs:
        if run.elevation_gain_m is not None:
            months = by_year.setdefault(run.start.year, [0.0] * 12)
            months[run.start.month - 1] += run.elevation_gain_m
    return {year: [round(v) for v in months] for year, months in by_year.items()}


def start_hour_distribution(runs: Sequence[Run]) -> list[int]:
    """Local start hour (0-23) of each run."""
    return [r.local_hour for r in runs]


def cumulative_distance_by_year(
    runs: Sequence[Run],
) -> dict[int, list[tuple[int, float]]]:
    """Map year -> [(day_of_year, cumulative_km)] for a YTD overlay."""
    per_year: dict[int, list[tuple[int, float]]] = defaultdict(list)
    for run in runs:
        if run.distance_km is not None:
            per_year[run.start.year].append(
                (run.start.timetuple().tm_yday, run.distance_km)
            )
    result: dict[int, list[tuple[int, float]]] = {}
    for year, points in per_year.items():
        cumulative = 0.0
        curve: list[tuple[int, float]] = []
        for day, km in sorted(points):
            cumulative += km
            curve.append((day, round(cumulative, 1)))
        result[year] = curve
    return result
