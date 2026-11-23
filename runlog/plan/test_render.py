"""The plan/review markdown renderers produce the exact expected document."""

from __future__ import annotations

from runlog.plan import render
from runlog.plan.schema import (
    AppleWorkout,
    IntervalSet,
    PlanReview,
    Session,
    TrainingPlan,
    Week,
    WeekAdjustment,
    WorkoutStep,
)
from runlog.plan.targets import TrainingZone


def test_to_markdown_renders_zones_week_and_apple_workout() -> None:
    zones = [
        TrainingZone(
            kind="Easy",
            pace_fast_s=340.0,
            pace_slow_s=360.0,
            hr_low=133,
            hr_high=148,
            rpe_low=3,
            rpe_high=4,
            purpose="Aerobic base",
        )
    ]
    session = Session(
        day="Thursday",
        kind="Intervals",
        venue="Track",
        duration_min=40,
        target_pace="4:00/km",
        target_hr="168-178",
        target_rpe=8,
        description="4×3min",
        apple_workout=AppleWorkout(
            warm_up=WorkoutStep(label="Warm Up", goal="1 km"),
            sets=[
                IntervalSet(
                    repeat=4,
                    work=WorkoutStep(
                        label="Work",
                        goal="3:00",
                        pace_target="4:00-4:10/km",
                        hr_target="168-178",
                    ),
                    recovery=WorkoutStep(label="Recovery", goal="90s"),
                )
            ],
            cool_down=WorkoutStep(label="Cool Down", goal="1 km"),
        ),
    )
    plan = TrainingPlan(
        goal="10k",
        race_date=None,
        weeks_to_goal=8,
        summary="Base build.",
        weeks=[
            Week(
                week_number=1,
                focus="Base",
                total_km=30.0,
                sessions=[session],
                notes="Ease in.",
            )
        ],
        key_advice=["Sleep well."],
    )
    lines = [
        "# Training plan — 10k",
        "",
        "**Ongoing block**  ·  **8 weeks** (review and extend)",
        "",
        "Base build.",
        "",
        "## Your training zones (pace & HR targets from your data)",
        "",
        "| Zone | Pace | HR (bpm) | RPE | Purpose |",
        "| --- | --- | --- | --- | --- |",
        "| Easy | 5:40-6:00/km | 133-148 | 3-4 | Aerobic base |",
        "",
        "## Week 1 — Base  (30 km)",
        "",
        "| Day | Type | Venue | Load | Pace | HR | RPE | Session |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
        "| Thursday | Intervals | Track | 40 min | 4:00/km | 168-178 | 8 | 4×3min |",
        "",
        "_Ease in._",
        "",
        "**⌚ Thursday Intervals — Apple Watch custom workout**",
        "",
        "- Warm Up: 1 km",
        "- Repeat 4×:",
        "    - Work: 3:00 @ 4:00-4:10/km / 168-178",
        "    - Recovery: 90s",
        "- Cool Down: 1 km",
        "",
        "## Key advice",
        "",
        "- Sleep well.",
    ]
    assert render.to_markdown(plan, zones) == "\n".join(lines) + "\n"


def test_to_markdown_race_block_shows_race_date_horizon() -> None:
    plan = TrainingPlan(
        goal="5k",
        race_date="2026-09-01",
        weeks_to_goal=6,
        summary="Sharpen.",
        weeks=[],
        key_advice=[],
    )
    assert render.to_markdown(plan) == (
        "# Training plan — 5k\n"
        "\n"
        "**Race date:** 2026-09-01  ·  **6 weeks**\n"
        "\n"
        "Sharpen.\n"
    )


def test_review_to_markdown_renders_all_sections() -> None:
    review = PlanReview(
        summary="Good adherence.",
        adherence_pct=80,
        on_track=True,
        whats_working=["Consistent."],
        whats_off=["Easy too fast."],
        tips=["Slow down."],
        week_adjustments=[
            WeekAdjustment(
                week=1, focus="Base", adherence="on-track", adjustment="Hold."
            )
        ],
        watch_outs=["ACWR rising."],
    )
    lines = [
        "# Plan follow-up",
        "",
        "**Adherence:** 80% · **on track**",
        "",
        "## Adherence",
        "",
        "Good adherence.",
        "",
        "## What's working",
        "",
        "- Consistent.",
        "",
        "## What to fix",
        "",
        "- Easy too fast.",
        "",
        "## Weekly adjustments",
        "",
        "| Week | Focus | Adherence | Adjustment |",
        "| --- | --- | --- | --- |",
        "| 1 | Base | on-track | Hold. |",
        "",
        "## Tips",
        "",
        "- Slow down.",
        "",
        "## Watch-outs",
        "",
        "- ACWR rising.",
    ]
    assert render.review_to_markdown(review) == "\n".join(lines) + "\n"
