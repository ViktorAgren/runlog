"""Render a :class:`TrainingPlan` (and training zones) as markdown (pure)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from runlog.plan.schema import AppleWorkout, Session, TrainingPlan, WorkoutStep
    from runlog.plan.targets import TrainingZone


def _pace(seconds: float) -> str:
    minutes, secs = divmod(int(round(seconds)), 60)
    return f"{minutes}:{secs:02d}/km"


def _zone_table(zones: Sequence[TrainingZone]) -> list[str]:
    lines = [
        "## Your training zones (pace & HR targets from your data)",
        "",
        "| Zone | Pace | HR (bpm) | RPE | Purpose |",
        "| --- | --- | --- | --- | --- |",
    ]
    for zone in zones:
        pace = f"{_pace(zone.pace_fast_s)}-{_pace(zone.pace_slow_s)}".replace(
            "/km-", "-"
        )
        hr = f"{zone.hr_low}-{zone.hr_high}" if zone.hr_low is not None else "by feel"
        lines.append(
            f"| {zone.kind} | {pace} | {hr} | {zone.rpe_low}-{zone.rpe_high} "
            f"| {zone.purpose} |"
        )
    return lines


def _load(session: Session) -> str:
    if session.distance_km is not None:
        return f"{session.distance_km:g} km"
    if session.duration_min is not None:
        return f"{session.duration_min} min"
    return "-"


def _session_row(session: Session) -> str:
    cells = [
        session.day,
        session.kind,
        session.venue,
        _load(session),
        session.target_pace or "-",
        session.target_hr or "-",
        str(session.target_rpe) if session.target_rpe is not None else "-",
        session.description.replace("|", "\\|"),
    ]
    return "| " + " | ".join(cells) + " |"


def _step_line(step: WorkoutStep) -> str:
    targets = " / ".join(t for t in (step.pace_target, step.hr_target) if t)
    suffix = f" @ {targets}" if targets else ""
    return f"{step.label}: {step.goal}{suffix}"


def _apple_block(day: str, kind: str, workout: AppleWorkout) -> list[str]:
    lines = [f"**⌚ {day} {kind} — Apple Watch custom workout**", ""]
    if workout.warm_up is not None:
        lines.append(f"- {_step_line(workout.warm_up)}")
    for interval in workout.sets:
        lines.append(f"- Repeat {interval.repeat}×:")
        lines.append(f"    - {_step_line(interval.work)}")
        if interval.recovery is not None:
            lines.append(f"    - {_step_line(interval.recovery)}")
    if workout.cool_down is not None:
        lines.append(f"- {_step_line(workout.cool_down)}")
    return lines


def to_markdown(plan: TrainingPlan, zones: Sequence[TrainingZone] = ()) -> str:
    """Render the full plan as a markdown document."""
    lines: list[str] = [
        f"# Training plan — {plan.goal}",
        "",
        f"**Race date:** {plan.race_date}  ·  **{plan.weeks_to_goal} weeks**",
        "",
        plan.summary,
    ]
    if zones:
        lines += ["", *_zone_table(zones)]

    for week in plan.weeks:
        lines += [
            "",
            f"## Week {week.week_number} — {week.focus}  ({week.total_km:g} km)",
            "",
            "| Day | Type | Venue | Load | Pace | HR | RPE | Session |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        lines += [_session_row(session) for session in week.sessions]
        if week.notes:
            lines += ["", f"_{week.notes}_"]
        for session in week.sessions:
            if session.apple_workout is not None:
                lines += [
                    "",
                    *_apple_block(session.day, session.kind, session.apple_workout),
                ]

    if plan.key_advice:
        lines += ["", "## Key advice", ""]
        lines += [f"- {advice}" for advice in plan.key_advice]
    return "\n".join(lines) + "\n"
