"""Network-free tests for the plan follow-up review (prompt, generate, render)."""

from __future__ import annotations

from datetime import date
from typing import Any

from runlog.plan import targets
from runlog.plan.generate import generate_review
from runlog.plan.profile import AthleteProfile
from runlog.plan.progress import PlanWeek, ProgressReport, WorkoutDetail
from runlog.plan.prompt import build_review_message
from runlog.plan.render import review_to_markdown
from runlog.plan.schema import PlanReview, WeekAdjustment

_PLAN_MD = "# Training plan — 3k\n\n## Week 1 — Base (38 km)\nEasy runs."


def _profile() -> AthleteProfile:
    return AthleteProfile(
        run_count=270,
        total_km=2120.0,
        avg_weekly_km=35.0,
        recent_weekly_km=[38.0, 36.0],
        runs_per_week=4.2,
        longest_run_km=17.0,
        longest_layoff_days=10,
        typical_pace_s_per_km=339.0,
        hr_max=190.0,
        hr_rest=50.0,
        vdot=45.0,
        zones=targets.build_training_zones(45.0, 190.0, 50.0),
    )


def _progress() -> ProgressReport:
    return ProgressReport(
        start=date(2026, 7, 5),
        today=date(2026, 7, 19),
        weeks_elapsed=2,
        total_runs=8,
        total_km=74.0,
        weekly_km=[38.0, 36.0],
        runs_per_week=4.0,
        longest_run_km=11.0,
        median_pace_s_per_km=330.0,
        easy_pct=8.0,
        moderate_pct=51.0,
        hard_pct=41.0,
        ctl_start=44.0,
        ctl_now=47.0,
        tsb_now=-9.0,
        acwr_now=1.06,
        red_flag_days=2,
        off_runs=1,
        workouts=[
            WorkoutDetail(
                day=date(2026, 7, 7),
                weekday="Tue",
                kind="Quality",
                distance_km=8.6,
                moving_s=2292,
                avg_pace_s_per_km=267.0,
                avg_hr=172.0,
                max_hr=189.0,
                easy_pct=12.0,
                moderate_pct=26.0,
                hard_pct=62.0,
                gap_pace_s_per_km=265.0,
                negative_split_pct=-1.2,
                elevation_gain_m=30.0,
            )
        ],
        plan_weeks=[PlanWeek(week=1, km=38.0, runs=6, kinds={"Quality": 2, "Easy": 4})],
    )


def test_build_review_message_includes_plan_progress_and_zones() -> None:
    message = build_review_message(_PLAN_MD, _progress(), _profile())
    assert "Week 1 — Base" in message  # verbatim plan
    assert "PROGRESS SINCE START" in message and "CTL 44 -> 47" in message
    assert "TRAINING ZONES" in message  # zone table for exact targets
    # Per-workout log: the plan-week rollup and the Tue quality session row.
    assert "PLAN-WEEK ROLLUP" in message and "Week 1: 38 km, 6 runs" in message
    assert "PER-WORKOUT LOG" in message and "| 2026-07-07 | Tue | Quality |" in message
    assert "12/26/62" in message  # its zone split


class _FakeMessages:
    def __init__(self, review: PlanReview) -> None:
        self._review = review
        self.captured: dict[str, Any] = {}

    def parse(self, **kwargs: Any) -> Any:
        self.captured = kwargs
        return type("R", (), {"parsed_output": self._review})()


class _FakeClient:
    def __init__(self, review: PlanReview) -> None:
        self.messages = _FakeMessages(review)


def _review() -> PlanReview:
    return PlanReview(
        summary="On track on volume, too little easy running.",
        adherence_pct=90,
        on_track=True,
        whats_working=["Volume matches the plan"],
        whats_off=["Only 8% easy — should be ~80%"],
        tips=["Slow easy runs to 6:02-6:51/km"],
        week_adjustments=[
            WeekAdjustment(week=2, focus="Build", adherence="partial", adjustment="")
        ],
        watch_outs=["2 red-flag days — watch resting HR"],
    )


def test_generate_review_returns_review_and_sends_prompt() -> None:
    review = _review()
    client = _FakeClient(review)

    result = generate_review(_PLAN_MD, _progress(), _profile(), client=client)

    assert result is review
    assert client.messages.captured["output_format"] is PlanReview
    assert "PROGRESS SINCE START" in client.messages.captured["messages"][0]["content"]


def test_review_to_markdown_renders_sections() -> None:
    markdown = review_to_markdown(_review())
    assert "**Adherence:** 90% · **on track**" in markdown
    assert "## Weekly adjustments" in markdown and "| 2 | Build | partial |" in markdown
    assert "## Tips" in markdown and "## Watch-outs" in markdown
