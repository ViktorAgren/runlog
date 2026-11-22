"""Race-time modelling: individualized fatigue exponent, VDOT, and an ensemble.

Three complementary models predict a race time from the athlete's own best
efforts: Riegel's power law ``T2 = T1 * (D2/D1)^k`` (the generic k = 1.06 is fit
to world records and flatters amateurs, so k is also fit to the athlete's own
efforts), the Daniels-Gilbert VDOT model, and the critical-speed model. The
ensemble reports their median and spread as a prediction interval.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from runlog.analyze.cs import CsModel

# Riegel's exponent fit to world records; the athlete's own value usually differs.
_GENERIC_EXPONENT = 1.06
# Human distance-runner VDOT never leaves this band; a value outside it is a
# degenerate effort/model, not fitness, so it is dropped rather than shown.
_PLAUSIBLE_VDOT = (20.0, 90.0)


def vdot(distance_m: float, seconds: float) -> float | None:
    """Daniels-Gilbert VDOT (VO2max-equivalent) from a race distance and time."""
    if distance_m <= 0 or seconds <= 0:
        return None
    minutes = seconds / 60
    velocity = distance_m / minutes  # m/min
    vo2 = -4.60 + 0.182258 * velocity + 0.000104 * velocity**2
    pct_max = (
        0.8
        + 0.1894393 * math.exp(-0.012778 * minutes)
        + 0.2989558 * math.exp(-0.1932605 * minutes)
    )
    if pct_max <= 0 or vo2 <= 0:
        return None
    value = vo2 / pct_max
    low, high = _PLAUSIBLE_VDOT
    return value if low <= value <= high else None


def fatigue_exponent(efforts: Sequence[tuple[float, float]]) -> float | None:
    """Riegel endurance exponent k: the slope of log(time) vs log(distance)."""
    points = [(d, t) for d, t in efforts if d > 0 and t > 0]
    if len({d for d, _ in points}) < 2:
        return None
    log_d = np.log([d for d, _ in points])
    log_t = np.log([t for _, t in points])
    return float(np.polyfit(log_d, log_t, 1)[0])


@dataclass(frozen=True)
class RacePrediction:
    distance_m: float
    seconds: float  # ensemble median
    low_s: float
    high_s: float
    methods: dict[str, float]


def _riegel(ref_d: float, ref_t: float, target_d: float, exponent: float) -> float:
    return float(ref_t * (target_d / ref_d) ** exponent)


def predict(
    efforts: Sequence[tuple[float, float]],
    distance_m: float,
    cs_model: CsModel | None = None,
) -> RacePrediction | None:
    """Ensemble race-time prediction (median of the models, min/max as interval)."""
    points = [(d, t) for d, t in efforts if d > 0 and t > 0]
    if not points:
        return None
    # Anchor on the effort nearest the target in log-distance (least extrapolation).
    ref_d, ref_t = min(points, key=lambda p: abs(math.log(p[0]) - math.log(distance_m)))
    methods: dict[str, float] = {
        "riegel_generic": _riegel(ref_d, ref_t, distance_m, _GENERIC_EXPONENT)
    }
    exponent = fatigue_exponent(points)
    if exponent is not None:
        methods["riegel_individual"] = _riegel(ref_d, ref_t, distance_m, exponent)
    if cs_model is not None:
        cs_time = cs_model.predict_seconds(distance_m)
        if cs_time is not None:
            methods["critical_speed"] = cs_time
    values = sorted(methods.values())
    return RacePrediction(
        distance_m=distance_m,
        seconds=statistics.median(values),
        low_s=values[0],
        high_s=values[-1],
        methods=methods,
    )


def vdot_series(
    cs_series: Sequence[tuple[date, CsModel]],
) -> list[tuple[date, float]]:
    """VDOT over time, from each window's critical-speed model's predicted 5 km."""
    series: list[tuple[date, float]] = []
    for day, model in cs_series:
        predicted_5k = model.predict_seconds(5000)
        if predicted_5k is None:
            continue
        value = vdot(5000, predicted_5k)
        if value is not None:
            series.append((day, round(value, 1)))
    return series
