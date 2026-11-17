"""Unit tests for the prose abstract."""

from __future__ import annotations

from datetime import date

from runlog.analyze import summary
from runlog.analyze.analytics import PmcPoint
from runlog.analyze.importance import Finding, SignificanceTable
from runlog.analyze.metrics import Summary
from runlog.coach.limiters import LimiterCard, LimiterFinding


def _overall() -> Summary:
    return Summary(
        run_count=162,
        total_km=1041.0,
        first_run=date(2026, 5, 1),
        last_run=date(2026, 11, 15),
        longest_km=20.0,
        this_week_km=66.0,
        this_month_km=200.0,
    )


def test_abstract_is_prose_with_period_fitness_and_top_finding() -> None:
    pmc = [PmcPoint(date(2026, 11, 15), fitness=71.0, fatigue=70.0, form=0.0)]
    finding = Finding(
        label="Aerobic efficiency trend",
        lane="training",
        kind="trend",
        effect="+0.011/30d",
        std_effect=1.0,
        ci="[0.009, 0.012]",
        p=0.0001,
        q=0.001,
        n=160,
        significant=True,
    )
    table = SignificanceTable(findings=(finding,), n_tests=28, alpha=0.05)
    text = summary.abstract(_overall(), pmc, table)
    assert "162 runs covered 1041 km" in text
    assert "fitness (CTL) is 71" in text
    assert "aerobic efficiency trend" in text
    assert "28 trend tests" in text
    assert "<pre" not in text and "=" * 10 not in text  # prose, not ASCII dump


def test_abstract_handles_missing_pieces() -> None:
    text = summary.abstract(_overall(), [], None)
    assert text.startswith("Between 01 May 2026")
    assert "CTL" not in text  # no PMC -> no fitness sentence


def test_limiters_section_renders_primary_and_prescriptions() -> None:
    card = LimiterCard(
        day=date(2026, 11, 17),
        primary="speed",
        findings=(
            LimiterFinding(
                "speed",
                3.0,
                ("short efforts +10% vs your CS curve",),
                "Weekly strides and hill sprints.",
            ),
            LimiterFinding(
                "engine", 0.0, ("VDOT 46.1 rising (+0.5/mo)",), "Add a 2nd quality day."
            ),
            LimiterFinding(
                "durability",
                0.0,
                ("fatigue resistance near reference",),
                "Progress the long run.",
            ),
            LimiterFinding(
                "structure",
                0.0,
                ("distribution & consistency on track",),
                "Hold the 80/20 structure.",
            ),
        ),
        confidence="high",
        confidence_note=None,
        short_gap_pct=10.0,
        long_gap_pct=0.0,
    )
    assert summary.limiters_section(card).splitlines() == [
        "",
        "Performance limiters",
        "=" * 40,
        "Primary: speed (severity 3.0/3, confidence high)",
        "Speed       3.0  short efforts +10% vs your CS curve",
        "            -> Weekly strides and hill sprints.",
        "Engine      0.0  VDOT 46.1 rising (+0.5/mo)",
        "Durability  0.0  fatigue resistance near reference",
        "Structure   0.0  distribution & consistency on track",
    ]
