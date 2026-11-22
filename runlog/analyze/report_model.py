"""Structured view model for the HTML report.

Plain frozen dataclasses that decouple *what* the report says (KPIs with
context, sections with a narrative + figures) from *how* it is rendered. The
orchestrator (:mod:`runlog.analyze.report`) fills this from the already computed
metrics; :mod:`runlog.analyze.html_report` renders it. Keeping it data-only
makes the report content unit-testable without touching matplotlib or jinja2.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from runlog.analyze.activities_view import ActivityRow

# Tone drives the KPI card accent in the template.
Tone = str  # "neutral" | "good" | "warn" | "bad"


@dataclass(frozen=True)
class Kpi:
    label: str
    value: str  # preformatted for display (e.g. "46.5", "5:39/km")
    unit: str = ""
    context: str = ""  # one short clause of context, e.g. "slightly fatigued"
    tone: Tone = "neutral"
    delta: str = ""  # preformatted change, e.g. "+12% vs 4-wk avg"
    delta_dir: str = ""  # "up" | "down" | "flat" — drives the arrow and its colour


@dataclass(frozen=True)
class TodayPlan:
    """The prescriptive top-of-report block: what to do today."""

    action: str  # GO | EASY | REST
    reason: str
    session: str = ""  # next planned session (kind + description), "" if no plan
    race: str = ""  # race-day outlook line, "" without a goal race


@dataclass(frozen=True)
class FigureRef:
    title: str
    path: str  # path relative to the report directory, e.g. "analytics/pmc.svg"
    caption: str = ""


@dataclass(frozen=True)
class Section:
    title: str
    narrative: str
    figures: list[FigureRef] = field(default_factory=list)


@dataclass(frozen=True)
class StatsRow:
    """One preformatted finding row for the what-matters table."""

    label: str
    lane: str
    effect: str
    ci: str
    p: str
    q: str
    n: str
    significant: bool


@dataclass(frozen=True)
class StatsTable:
    caption: str  # e.g. "28 tests, BH-FDR at q<0.05"
    rows: list[StatsRow] = field(default_factory=list)


@dataclass(frozen=True)
class ReportModel:
    title: str
    subtitle: str  # e.g. the date range
    generated_at: str
    kpis: list[Kpi]
    sections: list[Section]
    stats: StatsTable | None = None
    activities: list[ActivityRow] = field(default_factory=list)
    summary: str = ""  # the analyst narrative, shown as the report's abstract
    today: TodayPlan | None = None  # prescriptive guidance block, when available
    provenance: str = ""  # HR model and its source, shown under the status
    coverage: list[tuple[str, str]] = field(
        default_factory=list
    )  # what the report ran on


def acwr_tone(acwr: float) -> Tone:
    """Green inside the sweet spot, amber below, red above the injury line."""
    if acwr > 1.5:
        return "bad"
    if acwr < 0.8:
        return "warn"
    return "good"


def form_tone(tsb: float) -> Tone:
    """TSB: fresh (>5) good, deep negative (<-20) bad, otherwise neutral."""
    if tsb < -20:
        return "bad"
    if tsb > 5:
        return "good"
    return "neutral"
