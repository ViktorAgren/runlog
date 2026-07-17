"""Render coaching cards to compact plain text (no ANSI, aligned columns)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from runlog.analyze.forecast import format_race_time

if TYPE_CHECKING:
    from runlog.coach.daily import LastCard, TodayCard

_LABEL = 11  # label column width; values begin one space past it


def _row(label: str, value: str) -> str:
    return f"{label:<{_LABEL}} {value}"


def _pace(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    minutes, secs = divmod(int(round(seconds)), 60)
    return f"{minutes}:{secs:02d}"


def _clock(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def render_today(card: TodayCard) -> str:
    lines = [f"TODAY — {card.day:%a %Y-%m-%d}"]

    if card.readiness is not None:
        lines.append(_row("Readiness", f"{card.readiness.score:.1f}  (normal 40-60)"))
        markers = " · ".join(
            f"{name} {z:+.1f}" for name, z in card.readiness.contributors.items()
        )
        if markers:
            lines.append(_row("  markers", markers))
    else:
        lines.append(_row("Readiness", "—"))

    parts: list[str] = []
    if card.yesterday_trimp is not None:
        pct = (
            f" ({card.trimp_pctile:.0f}th pctile 90d)"
            if card.trimp_pctile is not None
            else ""
        )
        parts.append(f"TRIMP {card.yesterday_trimp:.0f}{pct}")
    if card.tsb is not None:
        parts.append(f"TSB {card.tsb:+.1f}")
    if card.acwr is not None:
        parts.append(f"ACWR {card.acwr:.2f}")
    lines.append(_row("Yesterday", " · ".join(parts) if parts else "—"))

    if card.session is not None:
        lines.append(_row("Plan", f"{card.session.kind} — {card.session.description}"))
    else:
        lines.append(_row("Plan", "no active plan schedule"))

    lines.append(
        _row("Guidance", f"{card.guidance.action} — {card.guidance.reasons[0]}")
    )

    if card.forecast is not None:
        f = card.forecast
        km = f.distance_m / 1000
        days = (f.race_day - card.day).days
        if f.ci_low_s is not None and f.ci_high_s is not None:
            band = (
                f" (95% CI {format_race_time(f.ci_low_s)}-"
                f"{format_race_time(f.ci_high_s)}, {f.method} n={f.n_weeks})"
            )
        else:
            band = f" ({f.method})"
        lines.append(
            _row(
                "Race",
                f"{km:.0f} km · {f.race_day} ({days} d) · "
                f"{format_race_time(f.predicted_s)}{band}",
            )
        )

    if card.fresh_records:
        recs = " · ".join(
            f"{r.scope.replace('_', '-')} {r.label}" for r in card.fresh_records
        )
        lines.append(_row("Records", recs))
    else:
        lines.append(_row("Records", "none in the last 7 days"))
    return "\n".join(lines)


def render_last(card: LastCard) -> str:
    d = card.detail
    hr = (
        f"HR {d.avg_hr:.0f}/{d.max_hr:.0f}"
        if d.avg_hr is not None and d.max_hr is not None
        else "HR —"
    )
    header = (
        f"LAST RUN — {d.day:%a %Y-%m-%d} · {d.kind} · {d.distance_km:.2f} km · "
        f"{_clock(d.moving_s)} · {_pace(d.avg_pace_s_per_km)}/km · {hr}"
    )
    lines = [header]

    if card.km_splits:
        lines.append(_row("Splits", "  ".join(_pace(s) for s in card.km_splits)))

    pacing: list[str] = []
    if d.gap_pace_s_per_km is not None:
        pacing.append(f"GAP {_pace(d.gap_pace_s_per_km)}/km")
    if d.negative_split_pct is not None:
        pacing.append(f"negative split {d.negative_split_pct:+.1f}%")
    if d.easy_pct is not None:
        pacing.append(
            f"zones E {d.easy_pct:.0f} / M {d.moderate_pct:.0f} / H {d.hard_pct:.0f} %"
        )
    if pacing:
        lines.append(_row("Pacing", " · ".join(pacing)))

    if card.comparison is not None:
        session = card.comparison.session
        lines.append(
            _row(
                "Plan", f"{session.kind} ({session.day:%b %-d}): {session.description}"
            )
        )
        avg_pace = _pace(d.avg_pace_s_per_km)
        pace_verdict = card.comparison.pace_verdict or "by feel — no band"
        lines.append(
            _row("  pace", f"{session.pace_text} → avg {avg_pace}  {pace_verdict}")
        )
        avg_hr = f"{d.avg_hr:.0f}" if d.avg_hr is not None else "—"
        hr_verdict = card.comparison.hr_verdict or "by feel — no band"
        lines.append(_row("  hr", f"{session.hr_text} → avg {avg_hr}  {hr_verdict}"))

    if card.fresh_records:
        recs = " · ".join(
            f"{r.scope.replace('_', '-')} {r.label}" for r in card.fresh_records
        )
        lines.append(_row("Records", recs))
    return "\n".join(lines)
