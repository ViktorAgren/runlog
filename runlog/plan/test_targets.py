"""Unit tests for VDOT / Karvonen target computation."""

from __future__ import annotations

import pytest

from runlog.plan import targets


def test_vdot_from_5k_matches_daniels_table() -> None:
    # A 20:00 5k is ~VDOT 50 in Daniels' tables.
    vdot = targets.vdot_from_effort(5000, 1200)
    assert vdot == pytest.approx(49.8, abs=1.0)


def test_threshold_pace_matches_daniels() -> None:
    # VDOT 50 threshold pace is ~4:15/km (255 s) in Daniels' tables.
    fast, slow = targets.training_paces(50.0)["Threshold"]
    assert fast == pytest.approx(255, abs=6)
    assert slow > fast  # slow end is a larger seconds/km value


def test_paces_get_faster_with_intensity() -> None:
    paces = targets.training_paces(50.0)
    # Easy is slower (bigger s/km) than Threshold, which is slower than Interval.
    assert paces["Easy"][0] > paces["Threshold"][0] > paces["Interval"][0]


def test_karvonen_band() -> None:
    # HRmax 190, rest 50, reserve 140. 0.7-0.8 -> 148-162.
    assert targets.heart_rate_reserve(190, 50, 0.70, 0.80) == (148, 162)


def test_build_training_zones_shape() -> None:
    zones = targets.build_training_zones(50.0, hr_max=190, hr_rest=50)
    kinds = [z.kind for z in zones]
    assert kinds == ["Recovery", "Easy", "Marathon", "Threshold", "Interval", "Rep"]
    threshold = next(z for z in zones if z.kind == "Threshold")
    assert (threshold.hr_low, threshold.hr_high) == (169, 176)  # 0.85/0.90 of reserve
    # Reps are steered by feel, not HR.
    assert next(z for z in zones if z.kind == "Rep").hr_low is None
