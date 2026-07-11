"""Pydantic models for the structured training plan Claude returns.

These become the JSON schema the model must fill (via ``messages.parse``), so
they are the contract for the plan. Keep to plain types — structured outputs
do not support string/number length constraints.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SessionKind = Literal[
    "Easy",
    "Long",
    "Tempo",
    "Intervals",
    "Recovery",
    "Strides",
    "Race",
    "Rest",
    "Cross-training",
]


class WorkoutStep(BaseModel):
    """One block of an Apple Watch custom workout."""

    label: Literal["Warm Up", "Work", "Recovery", "Cool Down"]
    goal: str = Field(description="Goal, e.g. '1 km', '3:00', or 'Open'")
    pace_target: str | None = Field(
        default=None, description="Pace alert, e.g. '4:05-4:15/km'"
    )
    hr_target: str | None = Field(
        default=None, description="HR alert in bpm, e.g. '168-176'"
    )


class IntervalSet(BaseModel):
    """A repeated work/recovery pair within a workout."""

    repeat: int = Field(description="How many times to repeat this pair")
    work: WorkoutStep
    recovery: WorkoutStep | None = None


class AppleWorkout(BaseModel):
    """A ready-to-build Apple Watch custom workout for a structured session."""

    warm_up: WorkoutStep | None = None
    sets: list[IntervalSet]
    cool_down: WorkoutStep | None = None


class Session(BaseModel):
    """One session on a specific day."""

    day: str = Field(description="Weekday, e.g. Monday")
    kind: SessionKind
    venue: str = Field(
        default="Road/GPS",
        description="Where to run, e.g. 'Track (optional)', 'Road/GPS', 'Treadmill'",
    )
    distance_km: float | None = Field(default=None, description="Target distance in km")
    duration_min: int | None = Field(
        default=None, description="Target duration in minutes"
    )
    target_pace: str | None = Field(
        default=None, description="Exact pace from the athlete's training zones"
    )
    target_hr: str | None = Field(
        default=None, description="Exact HR band in bpm from the zones, e.g. '168-176'"
    )
    target_rpe: int | None = Field(
        default=None, description="Perceived exertion 1-10 for this session"
    )
    description: str = Field(description="What to do, incl. interval structure")
    apple_workout: AppleWorkout | None = Field(
        default=None,
        description="For interval/tempo/track sessions: the watch workout blocks",
    )


class Week(BaseModel):
    """A single training week."""

    week_number: int
    focus: str = Field(description="Theme, e.g. 'Base building' or 'Taper'")
    total_km: float
    sessions: list[Session]
    notes: str = Field(description="Why this week is structured this way")


class TrainingPlan(BaseModel):
    """A full, dated training plan for a goal race."""

    goal: str
    race_date: str
    weeks_to_goal: int
    summary: str = Field(
        description="Overall approach, grounded in the athlete's current fitness"
    )
    weeks: list[Week]
    key_advice: list[str] = Field(description="A few high-priority reminders")


class WeekAdjustment(BaseModel):
    """A light tweak to one planned week — never a rewrite of the plan."""

    week: int = Field(description="Plan week number this note refers to")
    focus: str = Field(description="That week's planned focus, as read from the plan")
    adherence: Literal["ahead", "on-track", "partial", "missed"]
    adjustment: str = Field(
        description="Concrete tweak to make, or '' if the week is fine as planned"
    )


class PlanReview(BaseModel):
    """A coaching follow-up on a plan in progress. Tips only — no plan rewrite."""

    summary: str = Field(
        description="Overall adherence and fitness trajectory vs. the plan"
    )
    adherence_pct: int = Field(
        description="Rough %% of planned sessions/volume completed so far (0-100)"
    )
    on_track: bool
    whats_working: list[str]
    whats_off: list[str]
    tips: list[str] = Field(
        description="Prioritized, concrete tips that copy exact zone paces/HR"
    )
    week_adjustments: list[WeekAdjustment]
    watch_outs: list[str] = Field(
        description="Injury/overtraining risks to monitor (red-flag days, ACWR, etc.)"
    )
