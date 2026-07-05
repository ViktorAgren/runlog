"""Unit tests for prompt assembly and markdown rendering."""

from __future__ import annotations

from datetime import date

from runlog.plan.profile import AthleteProfile, PlanRequest
from runlog.plan.prompt import build_user_message, dry_run_text
from runlog.plan.render import to_markdown
from runlog.plan.schema import (
    AppleWorkout,
    IntervalSet,
    Session,
    TrainingPlan,
    Week,
    WorkoutStep,
)
from runlog.plan.targets import build_training_zones


def _profile() -> AthleteProfile:
    return AthleteProfile(
        run_count=270,
        total_km=1920.0,
        avg_weekly_km=34.0,
        recent_weekly_km=[38.0, 37.0, 45.0, 21.0],
        runs_per_week=4.5,
        longest_run_km=17.0,
        longest_layoff_days=14,
        typical_pace_s_per_km=330.0,
        best_efforts={"1k": 221.0, "5k": 1495.0},
        predicted_races={"5k": 1415.0},
        fitness_ctl=58.7,
        fatigue_atl=72.1,
        form_tsb=-11.5,
        acwr=1.06,
        efficiency_trend_per_month=0.013,
        vo2max=56.7,
        resting_hr=54.0,
        hr_max=190.0,
        hr_rest=54.0,
        vdot=42.0,
        zones=build_training_zones(42.0, hr_max=190, hr_rest=54),
    )


def _request() -> PlanRequest:
    return PlanRequest(
        goal="3k",
        race_date=date(2026, 9, 1),
        training_days=("Mon", "Wed", "Sat"),
        weeks_to_goal=8,
        target_time="12:00",
        max_distance_km=8.0,
        max_time_min=60,
    )


def test_user_message_includes_goal_constraints_and_data() -> None:
    message = build_user_message(_profile(), _request())
    assert "GOAL: 3k (target time 12:00)" in message
    assert "AVAILABLE TRAINING DAYS: Mon, Wed, Sat" in message
    assert "max 8 km per run; max 60 min per run" in message
    assert "Form(TSB) -11.5" in message
    assert "5k 24:55" in message  # 1495 s formatted as a best effort
    assert "TRAINING ZONES (use these EXACT targets" in message
    assert "- Threshold:" in message  # zone table with exact pace/HR/RPE


def test_dry_run_text_is_self_contained() -> None:
    text = dry_run_text(_profile(), _request())
    assert "=== SYSTEM (coach instructions) ===" in text
    assert "GOAL: 3k (target time 12:00)" in text
    assert "=== OUTPUT FORMAT ===" in text  # so a chat produces usable markdown


def test_to_markdown_renders_targets_and_apple_workout() -> None:
    plan = TrainingPlan(
        goal="3k",
        race_date="2026-09-01",
        weeks_to_goal=1,
        summary="A short sharpening block.",
        weeks=[
            Week(
                week_number=1,
                focus="Sharpen",
                total_km=18.0,
                sessions=[
                    Session(
                        day="Wednesday",
                        kind="Intervals",
                        venue="Track (optional)",
                        distance_km=6.0,
                        target_pace="4:00-4:10/km",
                        target_hr="168-176",
                        target_rpe=8,
                        description="6x400m @ 3k pace",
                        apple_workout=AppleWorkout(
                            warm_up=WorkoutStep(label="Warm Up", goal="1.5 km"),
                            sets=[
                                IntervalSet(
                                    repeat=6,
                                    work=WorkoutStep(
                                        label="Work",
                                        goal="400 m",
                                        pace_target="4:00-4:10/km",
                                        hr_target="168-176",
                                    ),
                                    recovery=WorkoutStep(
                                        label="Recovery", goal="200 m"
                                    ),
                                )
                            ],
                            cool_down=WorkoutStep(label="Cool Down", goal="1.5 km"),
                        ),
                    ),
                ],
                notes="Quality over volume this week.",
            )
        ],
        key_advice=["Sleep well"],
    )
    md = to_markdown(plan)
    row = "| Wednesday | Intervals | Track (optional) | 6 km | 4:00-4:10/km"
    assert row in md
    assert "| 168-176 | 8 |" in md
    assert "⌚ Wednesday Intervals — Apple Watch custom workout" in md
    assert "- Repeat 6×:" in md
    assert "- Work: 400 m @ 4:00-4:10/km / 168-176" in md
    assert "- Cool Down: 1.5 km" in md


def test_to_markdown_includes_zone_table() -> None:
    from runlog.plan.targets import build_training_zones

    plan = TrainingPlan(
        goal="3k",
        race_date="2026-09-01",
        weeks_to_goal=1,
        summary="s",
        weeks=[],
        key_advice=[],
    )
    zones = build_training_zones(50.0, hr_max=190, hr_rest=50)
    md = to_markdown(plan, zones)
    assert "## Your training zones" in md
    assert "| Threshold |" in md
