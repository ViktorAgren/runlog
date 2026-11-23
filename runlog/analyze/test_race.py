"""Unit tests for race-time modelling."""

from __future__ import annotations

from datetime import date

from runlog.analyze import cs, race


def test_vdot_matches_daniels_table() -> None:
    # Daniels: 5 km in 20:00 -> VDOT 50 (the formula gives 49.8).
    value = race.vdot(5000, 1200)
    assert value is not None and round(value, 1) == 49.8


def test_vdot_drops_physiologically_impossible_values() -> None:
    # A 5 km "in 90 s" implies a superhuman VDOT; it must be dropped, not shown.
    assert race.vdot(5000, 90) is None
    # A slow-but-real 5 km in 30 min scores inside the plausible band.
    assert race.vdot(5000, 1800) is not None


def test_vdot_series_tracks_each_window() -> None:
    models = [
        (
            date(2026, 6, 1),
            cs.fit_cs([cs.CsPoint(1000.0, 200.0), cs.CsPoint(5000.0, 1200.0)]),
        ),
        (
            date(2026, 7, 1),
            cs.fit_cs([cs.CsPoint(1000.0, 190.0), cs.CsPoint(5000.0, 1150.0)]),
        ),
    ]
    series = race.vdot_series([(day, m) for day, m in models if m is not None])
    assert [day for day, _ in series] == [date(2026, 6, 1), date(2026, 7, 1)]
    assert series[1][1] > series[0][1]  # the faster window scores higher


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
