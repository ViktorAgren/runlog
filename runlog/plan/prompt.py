"""Assemble the coach system prompt and the data-grounded user message.

Pure string assembly so it can be unit-tested without any API call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from runlog.plan.profile import AthleteProfile, PlanRequest

COACH_SYSTEM = (
    "You are an expert running coach. Design a safe, progressive, personalized "
    "training plan for the athlete's goal, grounded in the real data provided. "
    "Rules: (1) Prescribe sessions ONLY on the athlete's available training days; "
    "make every other day Rest or optional Cross-training. (2) Never exceed the "
    "athlete's stated max run distance or duration on any single run. (3) Build "
    "load progressively from their current Fitness (CTL) and keep the week-to-week "
    "jump conservative (roughly <=10%) so the acute:chronic ratio stays safe. "
    "(4) For EVERY session, set target_pace, target_hr, and target_rpe by "
    "assigning it a training zone and copying that zone's EXACT pace band, HR "
    "band, and RPE from the TRAINING ZONES table below — never invent numbers. "
    "Goal-race-pace work may be faster than the current Interval zone; introduce "
    "it progressively and label it clearly. (5) Set `venue` for each session: for "
    "key interval/rep sessions use 'Track (optional)' and give a road/GPS "
    "alternative in the description; otherwise 'Road/GPS' or 'Treadmill'. "
    "(6) For every Intervals/Tempo/Rep/track session, fill `apple_workout` — the "
    "athlete builds these as Apple Watch custom workouts, NOT Strava. A watchOS "
    "custom workout is an ordered list of blocks: a Warm Up, repeated Work/"
    "Recovery pairs (use `sets` with a repeat count), and a Cool Down; put the "
    "pace and/or HR target on each Work step and a goal (distance/time/Open) on "
    "every step. Leave apple_workout null for Easy/Long/Recovery/Rest. (7) Taper "
    "into race day. Explain your reasoning in each week's notes and the summary. "
    "Fill every field of the required structured format."
)


_FORMAT_HINT = (
    "Produce the plan as markdown: a short summary grounded in the data, then a "
    "'## Your training zones' table (Zone | Pace | HR | RPE | Purpose) copied "
    "from the TRAINING ZONES above. Then one section per week titled "
    "'## Week N - <focus> (<km> km)' with a table "
    "Day | Type | Venue | Load | Pace | HR | RPE | Session — copy the exact "
    "pace/HR/RPE from the zones and tag venue (Track (optional) for key reps, "
    "else Road/GPS) — followed by a one-line rationale. For each interval/tempo/"
    "track session add an '⌚ Apple Watch custom workout' block listing Warm Up, "
    "Repeat N× (Work @ pace/HR · Recovery), and Cool Down. Finish with "
    "'## Key advice'."
)


def dry_run_text(profile: AthleteProfile, request: PlanRequest) -> str:
    """The full prompt as pasteable text (for Claude Code / claude.ai)."""
    return "\n\n".join(
        [
            "=== SYSTEM (coach instructions) ===",
            COACH_SYSTEM,
            "=== REQUEST + ATHLETE DATA ===",
            build_user_message(profile, request),
            "=== OUTPUT FORMAT ===",
            _FORMAT_HINT,
        ]
    )


def _clock(seconds: float) -> str:
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _pace(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    minutes, secs = divmod(int(round(seconds)), 60)
    return f"{minutes}:{secs:02d}/km"


def _efforts(efforts: dict[str, float]) -> str:
    if not efforts:
        return "none recorded"
    return ", ".join(f"{label} {_clock(sec)}" for label, sec in efforts.items())


def _zones_block(profile: AthleteProfile) -> str:
    if not profile.zones:
        return "TRAINING ZONES: unavailable (no best-effort data)."
    header = (
        f"TRAINING ZONES (use these EXACT targets; VDOT {profile.vdot}, "
        f"HRmax {profile.hr_max:g}, resting {profile.hr_rest:g}):"
    )
    rows = [header]
    for zone in profile.zones:
        hr = f"{zone.hr_low}-{zone.hr_high} bpm" if zone.hr_low else "by feel"
        rows.append(
            f"- {zone.kind}: {_pace(zone.pace_fast_s)}-{_pace(zone.pace_slow_s)}, "
            f"HR {hr}, RPE {zone.rpe_low}-{zone.rpe_high} ({zone.purpose})"
        )
    return "\n".join(rows)


def build_user_message(profile: AthleteProfile, request: PlanRequest) -> str:
    """Serialize the goal, constraints, and athlete data into the user turn."""
    goal = request.goal
    if request.target_time:
        goal += f" (target time {request.target_time})"
    limits = []
    if request.max_distance_km is not None:
        limits.append(f"max {request.max_distance_km:g} km per run")
    if request.max_time_min is not None:
        limits.append(f"max {request.max_time_min} min per run")
    limit_text = "; ".join(limits) if limits else "none stated"

    return "\n".join(
        [
            f"GOAL: {goal}",
            f"RACE DATE: {request.race_date} ({request.weeks_to_goal} weeks away)",
            f"AVAILABLE TRAINING DAYS: {', '.join(request.training_days)}",
            f"CONSTRAINTS: {limit_text}",
            "",
            "ATHLETE PROFILE (from logged running data):",
            f"- History: {profile.run_count} runs, {profile.total_km:g} km total",
            f"- Weekly volume (last 90 days): {profile.avg_weekly_km:g} km average; "
            f"recent 8 weeks {profile.recent_weekly_km}",
            f"- Training cadence (last 90 days): {profile.runs_per_week:g} runs/week; "
            f"longest run {profile.longest_run_km:g} km (all-time); "
            f"longest recent layoff {profile.longest_layoff_days} days",
            f"- Typical recent pace: {_pace(profile.typical_pace_s_per_km)}",
            f"- Best efforts (continuous): {_efforts(profile.best_efforts)}",
            f"- Riegel predictions: {_efforts(profile.predicted_races)}",
            f"- Training status: Fitness(CTL) {profile.fitness_ctl}, "
            f"Fatigue(ATL) {profile.fatigue_atl}, Form(TSB) {profile.form_tsb}, "
            f"ACWR {profile.acwr}",
            f"- Efficiency trend: {profile.efficiency_trend_per_month} per month",
            f"- VO2max {profile.vo2max}, resting HR {profile.resting_hr}",
            "",
            _zones_block(profile),
        ]
    )
