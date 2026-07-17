"""Test that the HTML report is self-contained and shows the model's content."""

from __future__ import annotations

from pathlib import Path

from runlog.analyze import html_report, style
from runlog.analyze.report_model import (
    FigureRef,
    Kpi,
    ReportModel,
    Section,
    StatsRow,
    StatsTable,
)


def _write_figure(out_dir: Path, rel: str) -> None:
    path = out_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = style.figure("t")
    ax.plot([0, 1], [0, 1])
    style.save(fig, path.parent, path.name)


def test_render_embeds_figures_and_kpis(tmp_path: Path) -> None:
    _write_figure(tmp_path, "analytics/pmc.png")
    model = ReportModel(
        title="Running analytics report",
        subtitle="2025-01-01 to 2026-07-01 · 270 runs",
        generated_at="2026-07-10 09:00",
        kpis=[Kpi("Fitness (CTL)", "46", context="form -9 TSB", tone="neutral")],
        sections=[
            Section(
                "Fitness & form",
                "Fitness/fatigue balance.",
                [FigureRef("Fitness · Fatigue · Form", "analytics/pmc.png")],
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
        'src="data:image/png;base64,' in html,
        "analytics/pmc.png" not in html,  # path not referenced, only embedded
        "What matters (FDR-corrected)" in html,
        '<table class="stats">' in html,
        'class="sig"' in html,  # the significant row carries the accent
    )
    assert checks == (True, True, True, True, True, True, True)
