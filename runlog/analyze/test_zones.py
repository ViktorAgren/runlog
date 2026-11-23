"""The canonical zone table is well-formed and plan/report stay aligned."""

from __future__ import annotations

from runlog.analyze import metrics, zones
from runlog.plan import targets


def test_zones_tile_without_gap_or_overlap() -> None:
    assert [z.label for z in zones.ZONES] == ["Z1", "Z2", "Z3", "Z4", "Z5"]
    for lower, upper in zip(zones.ZONES, zones.ZONES[1:], strict=False):
        assert lower.high_frac is not None
        assert lower.high_frac == upper.low_frac  # contiguous, no gap/overlap
        assert lower.low_frac < lower.high_frac  # monotonic
    assert zones.ZONES[0].low_frac == 0.50
    assert zones.ZONES[-1].high_frac is None  # Z5 open-topped


def test_lactate_landmarks_sit_on_zone_boundaries() -> None:
    assert zones.ZONES[1].high_frac == zones.LT1_RESERVE_FRACTION  # Z2/Z3
    assert zones.ZONES[3].high_frac == zones.LT2_RESERVE_FRACTION  # Z4/Z5


def test_training_type_reserve_maps_each_type_to_one_zone() -> None:
    assert zones.training_type_reserve("Recovery") == (0.50, 0.60)
    assert zones.training_type_reserve("Easy") == (0.60, 0.70)
    assert zones.training_type_reserve("Marathon") == (0.70, 0.80)
    assert zones.training_type_reserve("Threshold") == (0.80, 0.90)
    assert zones.training_type_reserve("Interval") == (0.90, 1.00)  # open top capped
    assert zones.training_type_reserve("Rep") is None  # by feel


def test_plan_hr_bands_match_report_zones() -> None:
    # The whole point: a plan training type's HR band is exactly its Karvonen
    # zone from the report, so an in-band run never straddles a reporting edge.
    hr_max, hr_rest = 193, 43
    bounds = {
        label: (low, high)
        for label, low, high in metrics.hr_zone_bounds(hr_max, hr_rest)
    }
    built = {
        z.kind: (z.hr_low, z.hr_high)
        for z in targets.build_training_zones(50.0, hr_max=hr_max, hr_rest=hr_rest)
    }
    for kind, label in zones.TRAINING_TYPE_ZONE.items():
        if label is None:
            assert built[kind] == (None, None)
            continue
        low, high = bounds[label]
        expected_high = high if high is not None else hr_max  # Z5 open -> HR max
        assert built[kind] == (low, expected_high)
