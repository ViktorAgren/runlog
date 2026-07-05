"""Compute training targets from data: VDOT paces (Daniels) + HR zones (Karvonen).

Pure and deterministic — these are the authoritative paces/HR/RPE the plan
displays, so the model only assigns a zone to a session rather than inventing
numbers. VDOT is derived from a best effort; HR zones from max + resting HR.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Daniels' VO2 demand of running (v in m/min) and %VO2max as a function of time.
_VO2_A = 0.000104
_VO2_B = 0.182258
_VO2_C = -4.60

# Per training type: pace fractions of VDOT, HR fractions of reserve (Karvonen),
# RPE band, and purpose. HR fractions are None for Reps (too short to steer by HR).
_ZONE_SPECS: tuple[
    tuple[str, float, float, float | None, float | None, int, int, str], ...
] = (
    ("Recovery", 0.55, 0.63, 0.50, 0.60, 2, 3, "Easy shakeout / recovery"),
    ("Easy", 0.63, 0.74, 0.65, 0.78, 3, 4, "Aerobic base, conversational"),
    ("Marathon", 0.75, 0.84, 0.78, 0.85, 4, 5, "Aerobic strength"),
    ("Threshold", 0.83, 0.88, 0.85, 0.90, 6, 7, "Lactate threshold / tempo"),
    ("Interval", 0.95, 1.00, 0.92, 1.00, 8, 9, "VO2max repeats"),
    ("Rep", 1.05, 1.11, None, None, 9, 10, "Speed & economy (by feel)"),
)


@dataclass(frozen=True)
class TrainingZone:
    kind: str
    pace_fast_s: float  # seconds per km at the fast end of the band
    pace_slow_s: float
    hr_low: int | None
    hr_high: int | None
    rpe_low: int
    rpe_high: int
    purpose: str


def _vo2_at_velocity(v: float) -> float:
    return _VO2_C + _VO2_B * v + _VO2_A * v * v


def _velocity_at_vo2(target_vo2: float) -> float:
    """Positive root of the VO2 quadratic for a target oxygen cost (m/min)."""
    a, b, c = _VO2_A, _VO2_B, _VO2_C - target_vo2
    return (-b + math.sqrt(b * b - 4 * a * c)) / (2 * a)


def _pct_vo2max(time_min: float) -> float:
    return (
        0.8
        + 0.1894393 * math.exp(-0.012778 * time_min)
        + 0.2989558 * math.exp(-0.1932605 * time_min)
    )


def vdot_from_effort(distance_m: float, seconds: float) -> float:
    """Daniels VDOT from a maximal effort over ``distance_m`` in ``seconds``."""
    time_min = seconds / 60
    velocity = distance_m / time_min
    return _vo2_at_velocity(velocity) / _pct_vo2max(time_min)


def _pace_at_fraction(vdot: float, fraction: float) -> float:
    """Pace (s/km) at a given fraction of VDOT."""
    return 60000 / _velocity_at_vo2(vdot * fraction)


def training_paces(vdot: float) -> dict[str, tuple[float, float]]:
    """Per-type (fast, slow) pace band in s/km, from VDOT."""
    return {
        kind: (_pace_at_fraction(vdot, hi), _pace_at_fraction(vdot, lo))
        for kind, lo, hi, *_ in _ZONE_SPECS
    }


def heart_rate_reserve(
    hr_max: float, hr_rest: float, low_frac: float, high_frac: float
) -> tuple[int, int]:
    """Karvonen target HR band: rest + frac x (max - rest)."""
    reserve = hr_max - hr_rest
    return round(hr_rest + low_frac * reserve), round(hr_rest + high_frac * reserve)


def build_training_zones(
    vdot: float, hr_max: float, hr_rest: float
) -> list[TrainingZone]:
    """Assemble the authoritative pace + HR + RPE table for each training type."""
    zones: list[TrainingZone] = []
    for kind, plo, phi, hlo, hhi, rlo, rhi, purpose in _ZONE_SPECS:
        hr_low, hr_high = (
            heart_rate_reserve(hr_max, hr_rest, hlo, hhi)
            if hlo is not None and hhi is not None
            else (None, None)
        )
        zones.append(
            TrainingZone(
                kind=kind,
                pace_fast_s=_pace_at_fraction(vdot, phi),
                pace_slow_s=_pace_at_fraction(vdot, plo),
                hr_low=hr_low,
                hr_high=hr_high,
                rpe_low=rlo,
                rpe_high=rhi,
                purpose=purpose,
            )
        )
    return zones
