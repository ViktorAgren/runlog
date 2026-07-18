"""Assemble the Today and Last coaching cards, and the guidance rule.

Pure composition over the analysis layer and the parsed plan schedule — no I/O
beyond the DB connection it is handed. ``build_today`` answers "what should I
do this morning?"; ``build_last`` grades the most recent run against the plan.
The guidance rule is a deterministic first-match table so the recommendation
is explainable and testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from runlog.analyze import (
    analytics,
    forecast,
    metrics,
    readiness,
    records,
    streams,
)
from runlog.plan import progress

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence
    from datetime import date

    from runlog.analyze.forecast import RaceForecast
    from runlog.analyze.metrics import Run
    from runlog.analyze.readiness import ReadinessDay
    from runlog.analyze.records import RecordEvent
    from runlog.plan.progress import WorkoutDetail
    from runlog.plan.schedule import PlannedSession, PlanSchedule

# A recovery marker this far below its own baseline is a same-day red flag.
_RED_FLAG_Z = -2.0
_FRESH_RECORD_DAYS = 7
_TRIMP_WINDOW_DAYS = 90


@dataclass(frozen=True)
class Guidance:
    action: str  # "REST" | "EASY" | "GO"
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class TodayCard:
    day: date
    readiness: ReadinessDay | None
    yesterday_trimp: float | None
    trimp_pctile: float | None
    tsb: float | None
    acwr: float | None
    session: PlannedSession | None
    guidance: Guidance
    forecast: RaceForecast | None
    fresh_records: tuple[RecordEvent, ...]
    standing_records: tuple[RecordEvent, ...]  # current all-time bests


@dataclass(frozen=True)
class SessionComparison:
    session: PlannedSession
    pace_verdict: str | None  # "IN BAND" | "FAST OF BAND" | "SLOW OF BAND"
    hr_verdict: str | None  # "IN BAND" | "ABOVE BAND" | "BELOW BAND"


@dataclass(frozen=True)
class EffortLine:
    """This run's best effort at a distance, against the standing PB."""

    kind: str  # "1k" | "5k" | "10k"
    seconds: float
    pb_seconds: float | None
    is_pb: bool


@dataclass(frozen=True)
class LastCard:
    detail: WorkoutDetail
    km_splits: tuple[float, ...]
    comparison: SessionComparison | None
    efforts: tuple[EffortLine, ...]


def guidance_for(
    readiness_score: float | None,
    red_flag: bool,
    tsb: float | None,
    acwr: float | None,
    trimp_pctile: float | None,
    planned_rest: bool,
) -> Guidance:
    """Deterministic go/easy/rest call. First matching rule sets the action;
    every triggered rule contributes its reason."""
    reasons: list[tuple[str, str]] = []  # (action, reason)
    if planned_rest:
        reasons.append(("REST", "plan calls for rest"))
    if red_flag:
        reasons.append(("REST", "recovery marker red flag (z <= -2.0)"))
    if readiness_score is not None and readiness_score < 35:
        reasons.append(("REST", f"readiness {readiness_score:.0f} well below normal"))
    if readiness_score is not None and 35 <= readiness_score < 45:
        reasons.append(("EASY", f"readiness {readiness_score:.0f} below normal band"))
    if tsb is not None and tsb < -25:
        reasons.append(("EASY", f"deep fatigue (TSB {tsb:+.0f})"))
    if acwr is not None and acwr > 1.5:
        reasons.append(("EASY", f"load spike (ACWR {acwr:.2f} > 1.5)"))
    if (
        trimp_pctile is not None
        and trimp_pctile >= 90
        and readiness_score is not None
        and readiness_score < 55
    ):
        reasons.append(
            ("EASY", f"big day yesterday ({trimp_pctile:.0f}th pctile), not recovered")
        )
    green = (
        readiness_score is not None
        and readiness_score >= 55
        and (tsb is None or tsb > -15)
        and (acwr is None or acwr <= 1.3)
    )
    if green:
        reasons.append(("GO", "recovered and absorbing load"))

    order = {"REST": 0, "EASY": 1, "GO": 2}
    if reasons:
        action = min((a for a, _ in reasons), key=lambda a: order[a])
    else:
        action = "EASY"
        reasons.append(("EASY", "no green light — default easy"))
    return Guidance(action=action, reasons=tuple(r for _, r in reasons))


def _percentile(value: float, population: list[float]) -> float:
    """Share of the population at or below ``value`` (0-100)."""
    if not population:
        return 0.0
    at_or_below = sum(1 for v in population if v <= value)
    return round(at_or_below / len(population) * 100, 0)


def _resolve_hr_max(
    conn: sqlite3.Connection, runs: Sequence[Run], hr_max: float | None
) -> float:
    if hr_max is not None:
        return hr_max
    samples = metrics.hr_samples(conn, [r.activity_id for r in runs])
    return metrics.estimated_hr_max(samples)


def build_today(
    conn: sqlite3.Connection,
    schedule: PlanSchedule | None,
    today: date,
    hr_max: float | None = None,
) -> TodayCard:
    """The morning card: readiness, yesterday's strain, plan, guidance."""
    runs = metrics.canonical_run_activities(conn)
    hr_max_value = _resolve_hr_max(conn, runs, hr_max)
    hr_rest = metrics.resting_hr_median(conn)

    ready_days = readiness.readiness_series(conn)
    latest = ready_days[-1] if ready_days else None
    red_flag = latest is not None and any(
        z <= _RED_FLAG_Z for z in latest.contributors.values()
    )

    daily = analytics.daily_trimp(runs, hr_max_value, hr_rest)
    trimp_by_day = dict(daily)
    yesterday_trimp = trimp_by_day.get(today - timedelta(days=1))
    window = [
        load
        for day, load in daily
        if load > 0 and day >= today - timedelta(days=_TRIMP_WINDOW_DAYS)
    ]
    trimp_pctile = (
        _percentile(yesterday_trimp, window) if yesterday_trimp is not None else None
    )
    pmc = analytics.performance_management(daily)
    tsb = round(pmc[-1].form, 1) if pmc else None
    acwr_series = analytics.acwr_series(daily)
    acwr = acwr_series[-1][1] if acwr_series else None

    session = schedule.session_on(today) if schedule else None
    guidance = guidance_for(
        readiness_score=latest.score if latest else None,
        red_flag=red_flag,
        tsb=tsb,
        acwr=acwr,
        trimp_pctile=trimp_pctile,
        planned_rest=session.is_rest if session else False,
    )

    race_forecast = None
    if schedule is not None and schedule.race is not None:
        distance_m = schedule.race_distance_m
        if distance_m is not None:
            race_forecast = forecast.race_forecast(
                conn, runs, schedule.race.day, distance_m, today
            )

    timeline = records.records_timeline(conn, runs)
    fresh = records.new_records(timeline, today - timedelta(days=_FRESH_RECORD_DAYS))
    standing = records.current_records(timeline, "all_time")

    return TodayCard(
        day=today,
        readiness=latest,
        yesterday_trimp=round(yesterday_trimp, 0) if yesterday_trimp else None,
        trimp_pctile=trimp_pctile,
        tsb=tsb,
        acwr=acwr,
        session=session,
        guidance=guidance,
        forecast=race_forecast,
        fresh_records=tuple(fresh),
        standing_records=tuple(
            standing[k] for k in ("1k", "5k", "10k") if k in standing
        ),
    )


def compare_session(
    detail: WorkoutDetail, session: PlannedSession
) -> SessionComparison:
    """Grade a run's average pace and HR against the planned bands."""
    pace_verdict = None
    if (
        session.pace_low_s is not None
        and session.pace_high_s is not None
        and detail.avg_pace_s_per_km is not None
    ):
        pace = detail.avg_pace_s_per_km
        if pace < session.pace_low_s:
            pace_verdict = "FAST OF BAND"
        elif pace > session.pace_high_s:
            pace_verdict = "SLOW OF BAND"
        else:
            pace_verdict = "IN BAND"
    hr_verdict = None
    if (
        session.hr_low is not None
        and session.hr_high is not None
        and detail.avg_hr is not None
    ):
        hr = detail.avg_hr
        if hr > session.hr_high:
            hr_verdict = "ABOVE BAND"
        elif hr < session.hr_low:
            hr_verdict = "BELOW BAND"
        else:
            hr_verdict = "IN BAND"
    return SessionComparison(
        session=session, pace_verdict=pace_verdict, hr_verdict=hr_verdict
    )


def build_last(
    conn: sqlite3.Connection,
    schedule: PlanSchedule | None,
    today: date,
    hr_max: float | None = None,
) -> LastCard | None:
    """Post-run card for the most recent run; None when there are no runs."""
    runs = metrics.canonical_run_activities(conn)
    if not runs:
        return None
    hr_max_value = _resolve_hr_max(conn, runs, hr_max)
    run = max(runs, key=lambda r: r.start)
    detail = progress.workout_detail(conn, run, hr_max_value)
    km_splits = tuple(
        streams.km_split_paces(streams.full_stream(conn, run.activity_id))
    )

    comparison = None
    if schedule is not None:
        session = schedule.session_on(run.start.date())
        if session is not None:
            comparison = compare_session(detail, session)

    timeline = records.records_timeline(conn, runs)
    fresh_kinds = {e.kind for e in records.new_records(timeline, run.start.date())}
    standing = records.current_records(timeline, "all_time")
    stream_pts = [
        (s.offset_s, s.distance_m) for s in streams.full_stream(conn, run.activity_id)
    ]
    efforts: list[EffortLine] = []
    for kind, target in (("1k", 1000.0), ("5k", 5000.0), ("10k", 10000.0)):
        if run.distance_m is None or run.distance_m < target:
            continue
        seconds = analytics.best_effort_seconds(stream_pts, target)
        if seconds is None:
            continue
        pb = standing.get(kind)
        efforts.append(
            EffortLine(
                kind=kind,
                seconds=seconds,
                pb_seconds=pb.value if pb else None,
                is_pb=kind in fresh_kinds,
            )
        )
    return LastCard(
        detail=detail,
        km_splits=km_splits,
        comparison=comparison,
        efforts=tuple(efforts),
    )
