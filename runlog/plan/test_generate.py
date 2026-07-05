"""Network-free test for the generator via an injected fake client."""

from __future__ import annotations

from datetime import date
from typing import Any

from runlog.plan.generate import generate
from runlog.plan.profile import AthleteProfile, PlanRequest
from runlog.plan.schema import Session, TrainingPlan, Week


class _FakeResponse:
    def __init__(self, plan: TrainingPlan) -> None:
        self.parsed_output = plan


class _FakeMessages:
    def __init__(self, plan: TrainingPlan) -> None:
        self._plan = plan
        self.captured: dict[str, Any] = {}

    def parse(self, **kwargs: Any) -> _FakeResponse:
        self.captured = kwargs
        return _FakeResponse(self._plan)


class _FakeClient:
    def __init__(self, plan: TrainingPlan) -> None:
        self.messages = _FakeMessages(plan)


def _profile() -> AthleteProfile:
    return AthleteProfile(
        run_count=10,
        total_km=100.0,
        avg_weekly_km=20.0,
        recent_weekly_km=[20.0],
        runs_per_week=3.0,
        longest_run_km=10.0,
        longest_layoff_days=5,
        typical_pace_s_per_km=330.0,
    )


def test_generate_returns_plan_and_sends_grounded_prompt() -> None:
    plan = TrainingPlan(
        goal="3k",
        race_date="2026-09-01",
        weeks_to_goal=8,
        summary="ok",
        weeks=[
            Week(
                week_number=1,
                focus="Base",
                total_km=20.0,
                sessions=[
                    Session(day="Mon", kind="Easy", description="easy run"),
                ],
                notes="",
            )
        ],
        key_advice=[],
    )
    client = _FakeClient(plan)
    request = PlanRequest(
        goal="3k",
        race_date=date(2026, 9, 1),
        training_days=("Mon", "Wed", "Sat"),
        weeks_to_goal=8,
    )

    result = generate(_profile(), request, client=client, model="claude-opus-4-8")

    assert result is plan
    # The grounded user message and structured output_format were sent.
    assert client.messages.captured["output_format"] is TrainingPlan
    assert "GOAL: 3k" in client.messages.captured["messages"][0]["content"]
