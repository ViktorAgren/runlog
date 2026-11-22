"""Test that the HTML report is self-contained and shows the model's content."""

from __future__ import annotations

from pathlib import Path

from runlog.analyze import html_report, style
from runlog.analyze.activities_view import (
    ActivityDetail,
    ActivityRow,
    Chart,
    LapRow,
    ZoneBand,
)
from runlog.analyze.report_model import (
    FigureRef,
    Kpi,
    ReportModel,
    Section,
    StatsRow,
    StatsTable,
)


def _row() -> ActivityRow:
    return ActivityRow(
        activity_id=7,
        page="activities/7.html",
        date="2026-06-01",
        weekday="Mon",
        sport="Treadmill",
        indoor=True,
        session="Easy",
        source="runna",
        distance_km=5.0,
        duration_s=1500,
        duration_disp="25:00",
        pace_s=300.0,
        pace_disp="5:00/km",
        work_pace_s=None,
        avg_hr=150,
        max_hr=170,
        cadence=160,
        elevation_m=None,
        calories=300,
        load=52,
        ef=1.33,
        gap_s=None,
        gap_disp="-",
        decoupling_pct=1.2,
        easy_pct=80.0,
        moderate_pct=15.0,
        hard_pct=5.0,
        is_pr=True,
        is_off=False,
    )


def _write_figure(out_dir: Path, rel: str) -> None:
    path = out_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = style.figure("t")
    ax.plot([0, 1], [0, 1])
    style.save(fig, path.parent, path.name)


def test_render_embeds_figures_and_kpis(tmp_path: Path) -> None:
    _write_figure(tmp_path, "analytics/pmc.svg")
    model = ReportModel(
        title="Running analytics report",
        subtitle="2025-01-01 to 2026-07-01 · 270 runs",
        generated_at="2026-07-10 09:00",
        kpis=[Kpi("Fitness (CTL)", "46", context="form -9 TSB", tone="neutral")],
        sections=[
            Section(
                "Fitness & form",
                "Fitness/fatigue balance.",
                [FigureRef("Fitness · Fatigue · Form", "analytics/pmc.svg")],
            )
        ],
        stats=StatsTable(
            caption="28 tests, Benjamini-Hochberg FDR at q<0.05",
            rows=[
                StatsRow(
                    label="Walking HR trend",
                    lane="recovery",
                    effect="-0.31 bpm/30d",
                    ci="[-0.52, -0.11]",
                    p="p<.001",
                    q="0.004",
                    n="561",
                    significant=True,
                )
            ],
        ),
    )

    report = html_report.render(model, tmp_path)
    html = report.read_text(encoding="utf-8")

    assert report.name == "report.html"
    # KPI, section, figure, and stats table present; figure embedded.
    checks = (
        "Fitness (CTL)" in html,
        "Fitness &amp; form" in html,  # autoescaped section title
        "<svg" in html,  # the figure is inlined as vector SVG
        "analytics/pmc.svg" not in html,  # path not referenced, only embedded
        "Results — what matters" in html,
        '<table class="stats">' in html,
        'class="sig"' in html,  # the significant row carries the accent
    )
    assert checks == (True, True, True, True, True, True, True)


def test_render_includes_activities_tab_and_rows(tmp_path: Path) -> None:
    model = ReportModel(
        title="Running analytics report",
        subtitle="270 runs",
        generated_at="2026-07-10 09:00",
        kpis=[],
        sections=[],
        activities=[_row()],
    )
    html = html_report.render(model, tmp_path).read_text(encoding="utf-8")
    checks = (
        'data-tab="activities"' in html,  # the tab button
        'id="detail-frame"' in html,  # the detail iframe
        'id="detail-modal"' in html,  # rows open in a modal overlay
        "openActivity('activities/7.html'" in html,  # the row opens its page
        "25:00" in html,  # the row's vital numbers
    )
    assert checks == (True, True, True, True, True)
    analytical = (
        ">Load<" in html and ">GAP<" in html and ">EF<" in html,  # new columns
        '<span class="badge pr">PR</span>' in html,  # the PR row badge
        'class="ibar"' in html,  # the intensity bar
        'id="acts-summary"' in html,  # the live summary bar
    )
    assert analytical == (True, True, True, True)


def test_render_activity_pages_writes_a_page_with_chart_and_laps(
    tmp_path: Path,
) -> None:
    detail = ActivityDetail(
        activity_id=7,
        title="Treadmill · Easy · 2026-06-01",
        subtitle="Monday · runna",
        session="Easy",
        elevation_m=None,
        header=[("Distance", "5.00 km")],
        analysis=[("Pacing", "even (+0.2%)")],
        charts=[
            Chart(
                title="Heart rate",
                unit="bpm",
                color="#dc2626",
                y_format="int",
                points=[(0.0, 140.0), (1.0, 150.0)],
                min_span=20.0,
            )
        ],
        zones=[ZoneBand("Z2", "Endurance", "134-148", 900, 100)],
        thresholds=[("LT1 (aerobic)", "148 bpm")],
        intervals=None,
        laps=[LapRow(1, "1.00", "5:00", "5:00/km", 145, 160, 170)],
        load=52,
        ef=1.33,
        gap_s=None,
        decoupling_pct=1.2,
        easy_pct=80.0,
        moderate_pct=15.0,
        hard_pct=5.0,
    )

    pages = html_report.render_activity_pages([detail], tmp_path)

    assert [p.name for p in pages] == ["7.html"]
    html = pages[0].read_text(encoding="utf-8")
    checks = (
        "<svg" in html,  # the inline chart rendered
        "Heart rate (bpm)" in html,
        'class="laps"' in html,  # the laps table
        "5.00 km" in html,  # a header chip
        "Z2 · Endurance" in html,  # the zone band
        "LT1 (aerobic)" in html,  # a lactate-threshold estimate
    )
    assert checks == (True, True, True, True, True, True)


def test_render_shows_kpi_delta_and_training_status(tmp_path: Path) -> None:
    model = ReportModel(
        title="Running analytics report",
        subtitle="270 runs",
        generated_at="2026-07-10 09:00",
        kpis=[
            Kpi(
                "Fitness (CTL)",
                "75",
                context="form -1 TSB",
                delta="▲ 10 in 4 wk",
                delta_dir="good",
            )
        ],
        sections=[],
        summary="162 runs covered 1254 km; rolling volume up 18% over 4 weeks.",
        provenance="HR model: max 193 bpm (configured), rest 43 bpm (athlete.toml).",
    )
    html = html_report.render(model, tmp_path).read_text(encoding="utf-8")
    assert (
        'class="k-delta good"' in html
        and "▲ 10 in 4 wk" in html
        and "Training status" in html
        and "rolling volume up" in html
        and 'class="provenance"' in html
        and "rest 43 bpm (athlete.toml)" in html
    )
