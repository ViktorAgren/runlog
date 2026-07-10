"""Render a :class:`~runlog.analyze.report_model.ReportModel` to a self-contained
HTML dashboard.

Figures are inlined as base64 data URIs so the single ``report.html`` needs no
sibling assets and can be shared as one file. jinja2 does the templating; the
modern-dashboard CSS lives in the template.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

if TYPE_CHECKING:
    from runlog.analyze.report_model import ReportModel

_TEMPLATE_DIR = Path(__file__).parent / "templates"


def _data_uri(path: Path) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def render(model: ReportModel, out_dir: Path) -> Path:
    """Write ``out_dir/report.html`` with every figure embedded, return its path."""
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "j2"]),
    )
    template = env.get_template("report.html.j2")
    sections: list[dict[str, Any]] = []
    for section in model.sections:
        figures = [
            {"title": fig.title, "caption": fig.caption, "data_uri": _data_uri(path)}
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
