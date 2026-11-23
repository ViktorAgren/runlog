"""Canonical Karvonen heart-rate zones — the single source of truth.

Zone boundaries are a fraction of heart-rate *reserve* (``hr_max - hr_rest``),
not raw %HRmax: a plain %HRmax split pushes an efficient, low-resting-HR runner's
easy aerobic running up into the moderate band.

The report (via ``metrics._HR_ZONES``), the per-workout view (via
``activities_view``), and the training-plan generator (via ``plan.targets``) all
derive their HR bands from the tables here, so a run's zone means the same thing
everywhere. ``test_zones`` asserts the tables stay consistent.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Zone:
    """One Karvonen zone; ``high_frac`` is ``None`` for the open-topped Z5."""

    label: str
    name: str
    low_frac: float
    high_frac: float | None


ZONES: tuple[Zone, ...] = (
    Zone("Z1", "Recovery", 0.50, 0.60),
    Zone("Z2", "Endurance", 0.60, 0.70),
    Zone("Z3", "Tempo", 0.70, 0.80),
    Zone("Z4", "Threshold", 0.80, 0.90),
    Zone("Z5", "VO2max", 0.90, None),
)

# Lactate landmarks as reserve fractions, pinned to zone boundaries: LT1 (aerobic
# threshold) at the Z2/Z3 edge, LT2 (anaerobic threshold) at the Z4/Z5 edge.
LT1_RESERVE_FRACTION = 0.70
LT2_RESERVE_FRACTION = 0.90

# Each plan training type maps to exactly one Karvonen zone (``None`` = by feel),
# so a prescribed session's HR band is that zone's band and can never straddle a
# reporting boundary. Pace, not HR, still distinguishes the six types.
TRAINING_TYPE_ZONE: dict[str, str | None] = {
    "Recovery": "Z1",
    "Easy": "Z2",
    "Marathon": "Z3",
    "Threshold": "Z4",
    "Interval": "Z5",
    "Rep": None,
}

# Z5 is open-topped; a prescriptive band needs a finite ceiling (HR max).
_OPEN_TOP_FRACTION = 1.00


def _zone_by_label(label: str) -> Zone:
    for zone in ZONES:
        if zone.label == label:
            return zone
    raise KeyError(label)


def training_type_reserve(kind: str) -> tuple[float, float] | None:
    """(low, high) reserve fractions for a plan training type, or ``None`` (by feel)."""
    label = TRAINING_TYPE_ZONE[kind]
    if label is None:
        return None
    zone = _zone_by_label(label)
    high = zone.high_frac if zone.high_frac is not None else _OPEN_TOP_FRACTION
    return (zone.low_frac, high)
