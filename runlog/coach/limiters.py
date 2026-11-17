"""Diagnose the runner's performance limiter and rank it across four axes.

Deterministic and explainable, in the spirit of :func:`daily.guidance_for`: every
axis is scored to a common 0-3 severity and carries human-readable reasons, so the
read is auditable rather than a black box. The ranking spine is *expressed vs
potential* — the athlete's own best efforts measured against what their
critical-speed model predicts — so speed is localized from the runner's own curve
rather than an invented cut-off. Where a threshold is unavoidable (Seiler 80/20,
Foster monotony > 2.0, Friel decoupling > 5%, Riegel generic k = 1.06, ACWR
0.8-1.3) it is the published reference, cited in the reason.

Pure core (``diagnose``) with an imperative shell (``build_limiters``) that loads
the already-computed signals from the analysis layer and the athlete profile.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Literal

from runlog.analyze import analytics, cs, durability, metrics, physiology, race
from runlog.analyze.load import monotony_series
from runlog.plan.profile import build_profile

if TYPE_CHECKING:
    import sqlite3

    from runlog.analyze.cs import CsModel
    from runlog.config import Sex

LimiterAxis = Literal["engine", "speed", "durability", "structure"]
Confidence = Literal["high", "low"]

# The residual spine needs a shaped curve, not just a line, to localize speed.
_MIN_RESIDUAL_DISTANCES = 3
# Physiological primacy for a distance goal breaks severity ties.
_AXIS_RANK: dict[LimiterAxis, int] = {
    "engine": 0,
    "durability": 1,
    "speed": 2,
    "structure": 3,
}
# VDOT is a VO2max-equivalent, so a watch VO2max this far above the race-derived
# VDOT means the aerobic engine exists but is not being expressed in performance.
_UNDER_EXPRESSED = 5.0


@dataclass(frozen=True)
class LimiterFinding:
    axis: LimiterAxis
    severity: float  # 0 (not limiting) .. 3 (strongly limiting)
    reasons: tuple[str, ...]
    prescription: str


@dataclass(frozen=True)
class LimiterCard:
    day: date
    primary: LimiterAxis
    findings: tuple[LimiterFinding, ...]  # ranked, severity descending
    confidence: Confidence
    confidence_note: str | None  # what is missing, when confidence is low
    short_gap_pct: float | None  # residual at the shortest tested effort
    long_gap_pct: float | None  # residual at the longest tested effort


@dataclass(frozen=True)
class DiagnoseInput:
    """Pre-computed signals the pure diagnosis reasons over (parsed at the edge)."""

    vdot: float | None
    vo2max: float | None
    engine_trend_per_month: float | None
    cs_model: CsModel | None
    efforts: tuple[tuple[float, float], ...]  # (distance_m, best seconds)
    durability_fade_pct: float | None
    decoupling_pct: float | None
    fatigue_k: float | None
    easy_pct: float | None
    polarization_index: float | None
    monotony: float | None
    acwr: float | None
    runs_per_week: float | None
    longest_layoff_days: int | None


def _sev(value: float, zero: float, full: float) -> float:
    """Standardize to 0-3: ``zero`` maps to 0, ``full`` to 3, clamped between."""
    if full == zero:
        return 0.0
    scaled = 3.0 * (value - zero) / (full - zero)
    return round(max(0.0, min(3.0, scaled)), 2)


def _dist_label(distance_m: float) -> str:
    return {
        400.0: "400m",
        1000.0: "1k",
        2000.0: "2k",
        5000.0: "5k",
        10000.0: "10k",
    }.get(distance_m, f"{distance_m / 1000:.1f}k")


def _residuals(
    cs_model: CsModel | None, efforts: tuple[tuple[float, float], ...]
) -> list[tuple[float, float]]:
    """(distance, percent slower-than-predicted) for efforts inside the CS fit range.

    The critical-speed model has no fatigue term, so extrapolating it below or
    above the distances it was fit on is model bias, not athlete shape — a 10k
    always falls far short of the CS line. Only in-range efforts carry a real
    speed-vs-curve signal.
    """
    if cs_model is None or not cs_model.points:
        return []
    lo = min(p.distance_m for p in cs_model.points)
    hi = max(p.distance_m for p in cs_model.points)
    out: list[tuple[float, float]] = []
    for distance_m, seconds in sorted(efforts):
        if not lo <= distance_m <= hi:
            continue
        predicted = cs_model.predict_seconds(distance_m)
        if predicted is not None and predicted > 0:
            out.append((distance_m, (seconds - predicted) / predicted * 100))
    return out


def _engine_finding(inp: DiagnoseInput) -> LimiterFinding:
    prescription = (
        "Add a 2nd quality day: weekly LT2 threshold plus fortnightly vVO2max "
        "reps (3-5 min) to lift the aerobic ceiling."
    )
    trend = inp.engine_trend_per_month
    if trend is None:
        return LimiterFinding(
            "engine",
            0.0,
            ("engine trend unavailable (need 2+ CS windows)",),
            prescription,
        )
    severity = _sev(-trend, -0.2, 0.55)
    state = "falling" if trend < -0.2 else "flat" if trend <= 0.2 else "rising"
    head = f"VDOT {inp.vdot:.1f} {state}" if inp.vdot is not None else f"VDOT {state}"
    reason = f"{head} ({trend:+.1f}/mo)"
    if inp.vo2max is not None:
        reason += f" · VO2max {inp.vo2max:.1f}"
    reasons = [reason]
    if (
        inp.vdot is not None
        and inp.vo2max is not None
        and inp.vo2max - inp.vdot > _UNDER_EXPRESSED
    ):
        reasons.append(
            f"engine under-expressed: VO2max {inp.vo2max:.0f} vs race-VDOT "
            f"{inp.vdot:.0f} (+{inp.vo2max - inp.vdot:.0f}) — gains are in "
            "speed/economy/structure, not more aerobic volume"
        )
    return LimiterFinding("engine", severity, tuple(reasons), prescription)


def _speed_finding(short_gap: float | None, cs_model: CsModel | None) -> LimiterFinding:
    prescription = (
        "Weekly neuromuscular speed: strides / short reps (10-20 x 100-200 m) "
        "and hill sprints."
    )
    if short_gap is None:
        return LimiterFinding(
            "speed",
            0.0,
            ("speed vs curve unavailable (need 3+ effort distances)",),
            prescription,
        )
    severity = _sev(short_gap, 1.0, 7.0)
    reason = f"short-distance efforts {short_gap:+.0f}% vs your CS curve"
    if cs_model is not None:
        reason += f" · D' {cs_model.d_prime_m:.0f} m reserve"
    return LimiterFinding("speed", severity, (reason,), prescription)


def _durability_finding(inp: DiagnoseInput) -> LimiterFinding:
    prescription = (
        "Progress the long run and add tempo-finish long runs; hold weekly "
        "aerobic volume to build fatigue resistance."
    )
    drivers: list[tuple[float, str]] = []
    if inp.durability_fade_pct is not None:
        sev = _sev(inp.durability_fade_pct, 4.0, 13.0)
        if sev > 0:
            drivers.append(
                (sev, f"EF fades {inp.durability_fade_pct:.0f}% late in long runs")
            )
    if inp.decoupling_pct is not None:
        sev = _sev(inp.decoupling_pct, 5.0, 15.0)
        if sev > 0:
            drivers.append(
                (sev, f"HR-pace decoupling {inp.decoupling_pct:.0f}% (>5% Friel)")
            )
    if inp.fatigue_k is not None:
        sev = _sev(inp.fatigue_k, 1.06, 1.18)
        if sev > 0:
            drivers.append((sev, f"endurance exponent k={inp.fatigue_k:.2f} (>1.06)"))
    if not drivers:
        return LimiterFinding(
            "durability", 0.0, ("fatigue resistance near reference",), prescription
        )
    drivers.sort(reverse=True)
    severity = drivers[0][0]
    return LimiterFinding(
        "durability", severity, tuple(r for _, r in drivers), prescription
    )


def _structure_flags(inp: DiagnoseInput) -> list[tuple[float, str, str]]:
    """(severity, reason, fix) for each triggered structure rule."""
    flags: list[tuple[float, str, str]] = []
    if inp.easy_pct is not None and inp.easy_pct < 80:
        pi = (
            f", PI {inp.polarization_index:.1f}"
            if inp.polarization_index is not None
            else ""
        )
        flags.append(
            (
                _sev(80 - inp.easy_pct, 0.0, 21.0),
                f"only {inp.easy_pct:.0f}% easy (Seiler ~80%){pi}",
                "Shift to ~80/20: raise easy-run time, keep hard days hard.",
            )
        )
    if inp.monotony is not None and inp.monotony > 2.0:
        flags.append(
            (
                _sev(inp.monotony, 2.0, 3.5),
                f"Foster monotony {inp.monotony:.1f} (>2.0)",
                "Vary daily load: harder hard days, genuinely easy easy days.",
            )
        )
    if inp.acwr is not None and (inp.acwr < 0.8 or inp.acwr > 1.3):
        val = 0.8 - inp.acwr if inp.acwr < 0.8 else inp.acwr - 1.3
        flags.append(
            (
                _sev(val, 0.0, 0.6),
                f"ACWR {inp.acwr:.2f} outside 0.8-1.3",
                "Ramp weekly load gradually back into the 0.8-1.3 band.",
            )
        )
    if inp.runs_per_week is not None and inp.runs_per_week < 3:
        flags.append(
            (
                _sev(3 - inp.runs_per_week, 0.0, 3.0),
                f"{inp.runs_per_week:.1f} runs/wk (<3)",
                "Raise run frequency toward 4-5 easy-biased days.",
            )
        )
    if inp.longest_layoff_days is not None and inp.longest_layoff_days > 7:
        flags.append(
            (
                _sev(inp.longest_layoff_days - 7, 0.0, 21.0),
                f"{inp.longest_layoff_days}-day training gap",
                "Close the gaps: consistency compounds more than any single session.",
            )
        )
    return flags


def _structure_finding(inp: DiagnoseInput) -> LimiterFinding:
    flags = _structure_flags(inp)
    if not flags:
        return LimiterFinding(
            "structure",
            0.0,
            ("distribution & consistency on track",),
            "Hold the 80/20 structure and weekly consistency.",
        )
    flags.sort(reverse=True)
    severity = flags[0][0]
    return LimiterFinding(
        "structure", severity, tuple(r for _, r, _ in flags), flags[0][2]
    )


def diagnose(inp: DiagnoseInput, today: date | None = None) -> LimiterCard:
    """Rank the four limiter axes from pre-computed signals."""
    today = today or date.today()
    residuals = _residuals(inp.cs_model, inp.efforts)
    spine = len({d for d, _ in residuals}) >= _MIN_RESIDUAL_DISTANCES
    short_gap = residuals[0][1] if spine else None
    long_gap = residuals[-1][1] if spine else None

    findings = [
        _engine_finding(inp),
        _speed_finding(short_gap, inp.cs_model),
        _durability_finding(inp),
        _structure_finding(inp),
    ]
    findings.sort(key=lambda f: (-f.severity, _AXIS_RANK[f.axis]))

    confidence: Confidence = (
        "high" if spine and inp.engine_trend_per_month is not None else "low"
    )
    note = (
        _confidence_note(spine, inp.engine_trend_per_month)
        if confidence == "low"
        else None
    )

    return LimiterCard(
        day=today,
        primary=findings[0].axis,
        findings=tuple(findings),
        confidence=confidence,
        confidence_note=note,
        short_gap_pct=round(short_gap, 1) if short_gap is not None else None,
        long_gap_pct=round(long_gap, 1) if long_gap is not None else None,
    )


def _confidence_note(spine: bool, engine_trend: float | None) -> str:
    missing: list[str] = []
    if not spine:
        missing.append("best efforts at 3+ distances")
    if engine_trend is None:
        missing.append("2+ critical-speed windows for the engine trend")
    return "limited by missing " + " and ".join(missing)


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 1) if values else None


def build_limiters(
    conn: sqlite3.Connection,
    hr_max: float | None = None,
    hr_rest: float | None = None,
    sex: Sex | None = None,
    today: date | None = None,
) -> LimiterCard:
    """Load the signals and diagnose the runner's limiter (imperative shell)."""
    today = today or date.today()
    profile = build_profile(conn, hr_max, hr_rest, sex, today)
    runs = metrics.canonical_run_activities(conn)
    advanced = profile.advanced
    cs_model = advanced.critical_speed if advanced else None
    efforts = tuple(
        (r.distance_m, r.seconds)
        for r in (advanced.best_effort_records if advanced else [])
    )

    cs_series = cs.critical_speed_series(conn, runs, today)
    engine_trend_obj = analytics.linear_trend(race.vdot_series(cs_series))
    engine_trend = round(engine_trend_obj.per_30_days, 2) if engine_trend_obj else None

    fade = _median([v for _, v in durability.durability_series(conn, runs)])
    decoupling = _median(
        [v for _, v in (advanced.aerobic_decoupling if advanced else [])]
    )
    fatigue_k = race.fatigue_exponent(efforts)

    hr_max_value = profile.hr_max or 0.0
    hr_rest_value = profile.hr_rest or 0.0
    dist = physiology.training_intensity_distribution(
        conn, runs, hr_max_value, hr_rest_value
    )
    pol = (
        physiology.polarization_index(dist.easy_pct, dist.moderate_pct, dist.hard_pct)
        if dist
        else None
    )
    daily = analytics.daily_trimp(runs, hr_max_value, hr_rest_value, sex)
    monotony = monotony_series(daily)

    return diagnose(
        DiagnoseInput(
            vdot=profile.vdot,
            vo2max=profile.vo2max,
            engine_trend_per_month=engine_trend,
            cs_model=cs_model,
            efforts=efforts,
            durability_fade_pct=fade,
            decoupling_pct=decoupling,
            fatigue_k=fatigue_k,
            easy_pct=dist.easy_pct if dist else None,
            polarization_index=round(pol, 1) if pol is not None else None,
            monotony=round(monotony[-1][1], 1) if monotony else None,
            acwr=profile.acwr,
            runs_per_week=profile.runs_per_week,
            longest_layoff_days=profile.longest_layoff_days,
        ),
        today=today,
    )
