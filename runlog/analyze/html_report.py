"""Render a :class:`~runlog.analyze.report_model.ReportModel` to a self-contained
HTML dashboard.

Figures are inlined as base64 data URIs so the single ``report.html`` needs no
sibling assets and can be shared as one file. jinja2 does the templating; the
modern-dashboard CSS lives in the template.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from runlog.analyze import svg

if TYPE_CHECKING:
    from collections.abc import Sequence

    from runlog.analyze.activities_view import ActivityDetail
    from runlog.analyze.report_model import ReportModel

_TEMPLATE_DIR = Path(__file__).parent / "templates"


def _pace(seconds: float | None) -> str:
    """m:ss for a pace in s/km (template filter), or '-' when absent."""
    if seconds is None:
        return "-"
    total = int(round(seconds))
    return f"{total // 60}:{total % 60:02d}"


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "j2"]),
    )
    env.filters["pace"] = _pace
    return env


def _inline_svg(path: Path) -> str:
    """Return the figure's ``<svg>`` markup for inlining (drops the XML prolog)."""
    text = path.read_text(encoding="utf-8")
    start = text.find("<svg")
    return text[start:] if start != -1 else text


def render(model: ReportModel, out_dir: Path) -> Path:
    """Write ``out_dir/report.html`` with every figure embedded, return its path."""
    template = _env().get_template("report.html.j2")
    sections: list[dict[str, Any]] = []
    for section in model.sections:
        figures = [
            {"title": fig.title, "caption": fig.caption, "svg": _inline_svg(path)}
            for fig in section.figures
            if (path := out_dir / fig.path).exists()
        ]
        if figures or section.narrative:
            sections.append(
                {
                    "title": section.title,
                    "narrative": section.narrative,
                    "figures": figures,
                }
            )
    html = template.render(model=model, sections=sections)
    report_path = out_dir / "report.html"
    report_path.write_text(html, encoding="utf-8")
    return report_path


def render_activity_pages(
    details: Sequence[ActivityDetail], out_dir: Path
) -> list[Path]:
    """Write ``out_dir/activities/<id>.html`` per workout, return the paths."""
    template = _env().get_template("activity.html.j2")
    activities_dir = out_dir / "activities"
    activities_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for detail in details:
        charts = [
            svg.line_chart(
                chart.points,
                title=chart.title,
                unit=chart.unit,
                color=chart.color,
                y_format=chart.y_format,
                min_span=chart.min_span,
            )
            for chart in detail.charts
        ]
        html = template.render(detail=detail, charts=charts)
        page = activities_dir / f"{detail.activity_id}.html"
        page.write_text(html, encoding="utf-8")
        written.append(page)
    return written
