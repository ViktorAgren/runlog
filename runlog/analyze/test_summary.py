"""Unit tests for the prose abstract."""

from __future__ import annotations

from datetime import date

from runlog.analyze import summary
from runlog.analyze.analytics import PmcPoint
from runlog.analyze.importance import Finding, SignificanceTable
from runlog.analyze.metrics import Summary, WeeklyVolume
from runlog.analyze.physiology import IntensityDistribution
from runlog.analyze.readiness import ReadinessDay


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


def _finding() -> Finding:
    return Finding(
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


def test_training_status_narrates_volume_form_intensity_acwr_readiness() -> None:
    pmc = [
        PmcPoint(date(2026, 10, 1), 65.0, 60.0, 5.0),
        PmcPoint(date(2026, 11, 15), 75.0, 76.0, -1.0),
    ]
    weekly = [
        WeeklyVolume(
            week_start=date(2026, 10, w),
            distance_km=40.0,
            run_count=4,
            rolling_km=40.0 + w,
        )
        for w in range(1, 7)
    ]
    intensity = IntensityDistribution(
        easy_pct=82.0, moderate_pct=10.0, hard_pct=8.0, easy_to_hard=10.0
    )
    table = SignificanceTable(findings=(_finding(),), n_tests=28, alpha=0.05)

    text = summary.training_status(
        _overall(),
        pmc,
        weekly,
        intensity,
        [(date(2026, 11, 15), 1.1)],
        ReadinessDay(date(2026, 11, 15), 65.0, {}),
        None,
        table,
    )

    for fragment in (
        "162 runs covered 1041 km",
        "rolling volume up",
        "Fitness (CTL) is 75 and rising",
        "in the safe 0.8-1.3 band",
        "82% easy (well polarized",
        "65/100 (strong)",
        "strongest measured change",
    ):
        assert fragment in text


def test_training_status_degrades_to_the_period_line() -> None:
    text = summary.training_status(_overall(), [], [], None, [], None, None, None)
    assert text == "Between 01 May 2026 and 15 Nov 2026, 162 runs covered 1041 km."
