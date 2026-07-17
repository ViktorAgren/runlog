"""Unit tests for the FDR-corrected significance ranking."""

from __future__ import annotations

import math

from runlog.analyze import importance
from runlog.analyze.stats import CorrTest, GroupTest, TrendTest


def _trend(p: float, slope_per_day: float = 0.1, n: int = 100) -> TrendTest:
    return TrendTest(
        slope_per_day=slope_per_day,
        se_per_day=0.01,
        ci_low_per_day=slope_per_day - 0.02,
        ci_high_per_day=slope_per_day + 0.02,
        p=p,
        n=n,
    )


def _corr(p: float, r: float = 0.5) -> CorrTest:
    return CorrTest(r=r, ci_low=r - 0.2, ci_high=r + 0.2, p=p, n=100)


def _group(p: float, g: float = -1.1) -> GroupTest:
    return GroupTest(
        mean_a=1.0,
        mean_b=3.0,
        diff=-2.0,
        ci_low=-4.3,
        ci_high=0.3,
        p=p,
        df=8.0,
        hedges_g=g,
        n_a=5,
        n_b=5,
    )


def test_build_table_fdr_and_ordering() -> None:
    candidates = [
        importance.Candidate(
            "trend", "A trend", "recovery", "bpm/30d", trend=_trend(0.005), sd=3.0
        ),
        importance.Candidate("correlation", "B corr", "cross", "", corr=_corr(0.04)),
        importance.Candidate("contrast", "C contrast", "cross", "", group=_group(0.13)),
    ]
    table = importance.build_table(candidates)

    assert table.n_tests == 3
    assert [round(f.q, 3) for f in table.findings] == sorted(
        [round(v, 3) for v in (0.015, 0.06, 0.13)]
    )
    assert [f.significant for f in table.findings] == [True, False, False]
    # Only the trend survives FDR, so it leads regardless of effect size.
    assert table.findings[0].label == "A trend"


def test_standardized_effects() -> None:
    # Trend: slope 0.1/day -> 3.0 per 30d; sd 3.0 -> std effect 1.0.
    trend_candidate = importance.Candidate(
        "trend", "T", "recovery", "u/30d", trend=_trend(0.5), sd=3.0
    )
    # Correlation: r=0.5 -> 2*0.5/sqrt(0.75) = 1.1547.
    corr_candidate = importance.Candidate(
        "correlation", "C", "cross", "", corr=_corr(0.5)
    )
    table = importance.build_table([trend_candidate, corr_candidate])
    by_label = {f.label: f.std_effect for f in table.findings}
    assert math.isclose(by_label["T"], 1.0, abs_tol=1e-9)
    assert math.isclose(by_label["C"], 1.1547, abs_tol=1e-4)


def test_unscored_candidates_are_skipped() -> None:
    candidates = [
        importance.Candidate("trend", "no test", "recovery", "u", trend=None),
        importance.Candidate(
            "trend", "no sd", "recovery", "u", trend=_trend(0.01), sd=None
        ),
        importance.Candidate("correlation", "ok", "cross", "", corr=_corr(0.01)),
    ]
    table = importance.build_table(candidates)
    assert table.n_tests == 1
    assert table.findings[0].label == "ok"
