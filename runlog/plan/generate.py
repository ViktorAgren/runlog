"""Generate a validated training plan via the Anthropic API.

One outbound call. The client is injectable so the flow can be unit-tested with
a fake (no network); production uses ``anthropic.Anthropic()``, which reads
``ANTHROPIC_API_KEY`` from the environment / ``.env``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

import anthropic
from dotenv import load_dotenv

from runlog.plan.prompt import COACH_SYSTEM, build_user_message
from runlog.plan.schema import TrainingPlan

if TYPE_CHECKING:
    from runlog.plan.profile import AthleteProfile, PlanRequest

DEFAULT_MODEL = "claude-opus-4-8"
_MAX_TOKENS = 16000


class PlanClient(Protocol):
    """The slice of the Anthropic client this module uses."""

    @property
    def messages(self) -> Any: ...


def generate(
    profile: AthleteProfile,
    request: PlanRequest,
    client: PlanClient | None = None,
    model: str = DEFAULT_MODEL,
) -> TrainingPlan:
    """Ask Claude for a structured, data-grounded plan and validate it."""
    if client is None:
        load_dotenv()
        client = anthropic.Anthropic()
    response = client.messages.parse(
        model=model,
        max_tokens=_MAX_TOKENS,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        system=COACH_SYSTEM,
        messages=[{"role": "user", "content": build_user_message(profile, request)}],
        output_format=TrainingPlan,
    )
    plan = response.parsed_output
    if not isinstance(plan, TrainingPlan):
        raise RuntimeError("The model did not return a structured plan.")
    return plan
