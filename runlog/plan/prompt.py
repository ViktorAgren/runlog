"""Assemble the coach system prompt and the data-grounded user message.

Pure string assembly so it can be unit-tested without any API call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from runlog.plan.profile import AdvancedFitness, AthleteProfile, PlanRequest
    from runlog.plan.progress import ProgressReport, WorkoutDetail

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
    "into race day. (8) The TRAINING ZONES are derived from VDOT; cross-check "
    "them against the ADVANCED FITNESS block. If the critical-speed model or "
    "recent best efforts imply the athlete sustains faster paces than the VDOT "
    "zones suggest (e.g. CS predicts a race faster than the goal, or best "
    "efforts beat the zone paces), say so in the summary and lean the harder "
    "bands (Threshold/Interval/Rep/goal-pace) toward that measured evidence "
    "rather than the VDOT table — while keeping Easy/Recovery genuinely easy. "
    "Explain your reasoning in each week's notes and the summary. "
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
            *_advanced_lines(profile.advanced),
            "",
            _zones_block(profile),
        ]
    )


# --- Plan follow-up / coaching review ---------------------------------------

REVIEW_SYSTEM = (
    "You are an expert running coach reviewing an athlete's progress against a "
    "training plan they have been following. You are given the original plan "
    "verbatim, a PER-WORKOUT LOG of every run they actually did (with date, "
    "weekday, kind, distance, pace, HR, and HR-zone split), a PLAN-WEEK ROLLUP, "
    "and block aggregates. Rules: (1) Do a SESSION-BY-SESSION comparison — for "
    "each planned session, find the performed run on that date in the log and "
    "judge it: was the distance met, and did the average pace and HR land in the "
    "prescribed zone? Quote the specific date, pace, and HR. Note planned "
    "sessions with no matching run (missed) and extra unplanned runs. (2) Roll "
    "that up per plan week (planned km/sessions vs the PLAN-WEEK ROLLUP) into an "
    "adherence assessment and a completion %. (3) Ground EVERY claim in the log's "
    "numbers — never speak only in block averages, and never invent data. "
    "(4) Tips MUST copy the EXACT pace/HR bands from the TRAINING ZONES table. "
    "(5) Call out the biggest mismatches: easy days run too hard (check the "
    "zone split and pace vs the Easy band), quality sessions missing their "
    "target pace, or long runs cut short. (6) Suggest only light per-week tweaks; "
    "DO NOT rewrite the plan — if a full rewrite is warranted, say so in "
    "watch_outs and recommend re-running the planner. (7) Flag injury/"
    "overtraining risk: red-flag days, ACWR above 1.5, a sharp CTL drop, or a "
    "deeply negative Form (TSB). (8) Use the per-rep detail in the "
    "STRUCTURED-SESSION SPLITS: read each rep's avg/max HR and the 'set:' HR "
    "drift and pace CV to judge whether a quality set was controlled and even, "
    "or faded and ragged; read the per-workout Cad (cadence) and Drift (cardiac "
    "drift) columns and the ADVANCED FITNESS block (critical speed, best "
    "efforts, aerobic decoupling) to ground fitness claims. When the reps sit "
    "faster than the prescribed band at a controlled HR, or the critical-speed "
    "predictions beat the plan's targets, say the zones may read conservative. "
    "Keep everything concrete and prioritized; fill every field of the required "
    "structured format."
)

_REVIEW_FORMAT_HINT = (
    "Produce the review as markdown: '## Adherence' (an honest short paragraph "
    "plus the completion %), '## What's working' bullets, '## What to fix' "
    "bullets, a '## Weekly adjustments' table (Week | Focus | Adherence | "
    "Adjustment), '## Tips' (prioritized, copying exact zone paces/HR), and "
    "'## Watch-outs' (injury/overtraining risks). Do NOT rewrite the plan."
)


def _num(value: float | None, suffix: str = "") -> str:
    return "unknown" if value is None else f"{value:g}{suffix}"


def _progress_block(progress: ProgressReport) -> str:
    """Serialize the deterministic actuals-since-start into the user turn."""
    if progress.easy_pct is not None:
        mix = (
            f"easy {progress.easy_pct:g}% / moderate {progress.moderate_pct:g}% / "
            f"hard {progress.hard_pct:g}%"
        )
    else:
        mix = "unavailable (no HR streams)"
    return "\n".join(
        [
            f"PROGRESS SINCE START (plan began {progress.start}, now "
            f"{progress.today} — week {progress.weeks_elapsed} of the block):",
            f"- Runs: {progress.total_runs} totaling {progress.total_km:g} km; "
            f"longest {progress.longest_run_km:g} km",
            f"- Weekly km since start: {progress.weekly_km}",
            f"- Cadence: {progress.runs_per_week:g} runs/week",
            f"- Typical pace: {_pace(progress.median_pace_s_per_km)}",
            f"- Intensity split: {mix}",
            f"- Best efforts this block: {_efforts(progress.best_efforts)}",
            f"- Fitness: CTL {_num(progress.ctl_start)} -> {_num(progress.ctl_now)}; "
            f"Form(TSB) {_num(progress.tsb_now)}, ACWR {_num(progress.acwr_now)}",
            f"- Efficiency trend this block: "
            f"{_num(progress.efficiency_trend_per_month)} per month",
            f"- Flags: {progress.red_flag_days} readiness red-flag day(s), "
            f"{progress.off_runs} off-run(s) (slow for the effort)",
            *_advanced_lines(progress.advanced),
        ]
    )


def _advanced_lines(advanced: AdvancedFitness | None) -> list[str]:
    """Measured-performance context: critical speed, best efforts, decoupling."""
    if advanced is None:
        return []
    lines: list[str] = ["- ADVANCED FITNESS:"]
    cs_model = advanced.critical_speed
    if cs_model is not None:
        preds = " / ".join(
            f"{label} {_clock(seconds)}"
            for label, dist in (("3k", 3000.0), ("5k", 5000.0))
            if (seconds := cs_model.predict_seconds(dist)) is not None
        )
        lines.append(
            f"  - Critical speed {cs_model.cs_mps:.2f} m/s "
            f"(D' {cs_model.d_prime_m:g} m, r={cs_model.r:.2f}); predicts {preds}"
        )
    if advanced.best_effort_records:
        efforts = " · ".join(
            f"{rec.label} {_clock(rec.seconds)} ({_pace(rec.pace_s_per_km)})"
            for rec in advanced.best_effort_records
        )
        lines.append(f"  - Best continuous efforts: {efforts}")
    if advanced.aerobic_decoupling:
        recent = advanced.aerobic_decoupling[-3:]
        drift = ", ".join(f"{pct:+g}%" for _, pct in recent)
        lines.append(
            f"  - Aerobic decoupling (recent long runs, +ve = HR drifted up): "
            f"{drift}"
        )
    if advanced.avg_cadence is not None:
        lines.append(f"  - Average cadence: {advanced.avg_cadence:g} spm")
    return lines


def _hms(seconds: int | None) -> str:
    return _clock(seconds) if seconds else "-"


def _km(distance_km: float | None) -> str:
    return f"{distance_km:.1f}" if distance_km is not None else "-"


def _zone_split(workout: WorkoutDetail) -> str:
    if workout.easy_pct is None:
        return "-"
    return f"{workout.easy_pct:.0f}/{workout.moderate_pct:.0f}/{workout.hard_pct:.0f}"


def _lap_line(workout: WorkoutDetail) -> str:
    parts = []
    for lap in workout.laps:
        if lap.distance_m and lap.pace_s_per_km:
            piece = f"{lap.distance_m / 1000:.1f}km {_pace(lap.pace_s_per_km)}"
            if lap.avg_hr:
                piece += f" @{lap.avg_hr:.0f}"
                if lap.max_hr:
                    piece += f" max{lap.max_hr:.0f}"
            parts.append(piece)
        elif lap.seconds:
            parts.append(f"{lap.seconds}s")
    return " · ".join(parts)


def _rep_set_note(workout: WorkoutDetail) -> str:
    """A compact rep-set summary (HR drift across reps, pace evenness)."""
    if workout.rep_hr_drift is None and workout.rep_pace_cv is None:
        return ""
    bits = []
    if workout.rep_hr_drift is not None:
        bits.append(f"HR drift {workout.rep_hr_drift:+g} across reps")
    if workout.rep_pace_cv is not None:
        bits.append(f"pace CV {workout.rep_pace_cv:g}%")
    return f"  [set: {', '.join(bits)}]"


def _workout_log_block(progress: ProgressReport) -> str:
    """Per-plan-week rollup + a per-workout table + any structured-session splits."""
    lines = ["PLAN-WEEK ROLLUP (actual, bucketed from the start date):"]
    for week in progress.plan_weeks:
        tally = ", ".join(f"{k} {n}" for k, n in sorted(week.kinds.items()))
        lines.append(f"- Week {week.week}: {week.km:g} km, {week.runs} runs ({tally})")

    lines += [
        "",
        "PER-WORKOUT LOG (compare each to the plan's session for that date; "
        "E/M/H% = time in easy Z1-2 / moderate Z3 / hard Z4-5; Kind is inferred "
        "from HR-zone time, so a true interval session and a high-HR easy run can "
        "both read 'Quality' — weigh pace and the E/M/H% split too. Cad = avg "
        "cadence (spm); Drift = cardiac drift, +ve means HR rose for the same "
        "pace):",
        "| Date | Day | Kind | km | Time | Pace | avgHR | maxHR | Cad | Drift "
        "| E/M/H% | GAP |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for w in progress.workouts:
        drift = f"{w.cardiac_drift_pct:+g}%" if w.cardiac_drift_pct is not None else "-"
        lines.append(
            f"| {w.day} | {w.weekday} | {w.kind} "
            f"| {_km(w.distance_km)} | {_hms(w.moving_s)} "
            f"| {_pace(w.avg_pace_s_per_km)} "
            f"| {_num(w.avg_hr)} | {_num(w.max_hr)} | {_num(w.avg_cadence)} | {drift} "
            f"| {_zone_split(w)} "
            f"| {_pace(float(w.gap_pace_s_per_km)) if w.gap_pace_s_per_km else '-'} |"
        )

    splits = [(w, _lap_line(w)) for w in progress.workouts if w.laps]
    if splits:
        lines += ["", "STRUCTURED-SESSION SPLITS (laps as recorded):"]
        lines += [
            f"- {w.day} ({w.kind}): {line}{_rep_set_note(w)}"
            for w, line in splits
            if line
        ]
    return "\n".join(lines)


def build_review_message(
    plan_md: str, progress: ProgressReport, profile: AthleteProfile
) -> str:
    """Assemble the review user turn: the plan, the actuals, and the zones."""
    return "\n".join(
        [
            "=== ORIGINAL PLAN (verbatim) ===",
            plan_md.strip(),
            "",
            "=== ACTUAL TRAINING DATA ===",
            _progress_block(progress),
            "",
            _workout_log_block(progress),
            "",
            _zones_block(profile),
        ]
    )


def review_dry_run_text(
    plan_md: str, progress: ProgressReport, profile: AthleteProfile
) -> str:
    """The full review prompt as pasteable text (for Claude Code / claude.ai)."""
    return "\n\n".join(
        [
            "=== SYSTEM (coach review instructions) ===",
            REVIEW_SYSTEM,
            "=== PLAN + ACTUAL DATA ===",
            build_review_message(plan_md, progress, profile),
            "=== OUTPUT FORMAT ===",
            _REVIEW_FORMAT_HINT,
        ]
    )
