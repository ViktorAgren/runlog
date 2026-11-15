"""Unit tests for race-time modelling."""

from __future__ import annotations

from runlog.analyze import cs, race


def test_vdot_matches_daniels_table() -> None:
    # Daniels: 5 km in 20:00 -> VDOT 50 (the formula gives 49.8).
    value = race.vdot(5000, 1200)
    assert value is not None and round(value, 1) == 49.8


def test_fatigue_exponent_recovers_power_law() -> None:
    # time = a * distance^1.1 -> exponent 1.1.
    efforts = [(d, 0.5 * d**1.1) for d in (1000.0, 3000.0, 5000.0, 10000.0)]
    exponent = race.fatigue_exponent(efforts)
    assert exponent is not None and round(exponent, 3) == 1.1


def test_fatigue_exponent_none_single_distance() -> None:
    assert race.fatigue_exponent([(5000.0, 1200.0)]) is None


def test_predict_ensemble_combines_models() -> None:
    efforts = [(1000.0, 200.0), (5000.0, 1200.0)]
    model = cs.fit_cs([cs.CsPoint(distance_m=d, seconds=t) for d, t in efforts])
    prediction = race.predict(efforts, 10000.0, model)
    assert prediction is not None
    assert "riegel_generic" in prediction.methods
    assert "riegel_individual" in prediction.methods
    assert prediction.low_s <= prediction.seconds <= prediction.high_s
