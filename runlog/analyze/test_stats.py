"""Unit tests for the inferential statistics core.

Reference values were verified independently by Simpson integration of the
Student-t density (20k intervals) and direct computation of the Welch/Fisher
formulas, so these tests pin the implementation to known-correct numbers.
"""

from __future__ import annotations

import math
from datetime import date

import pytest

from runlog.analyze import stats


class TestStudentsT:
    @pytest.mark.parametrize(
        ("t", "df", "expected"),
        [
            (1.0, 1.0, 0.75),  # Cauchy closed form
            (0.0, 5.0, 0.5),
            (2.0, 10.0, 0.963306),
            (-2.0, 10.0, 0.036694),
            (2.5, 3.0, 0.956147),
            (3.0, 7.0, 0.990029),
            # Discriminates from the normal approximation (which gives 0.975002).
            (1.96, 1000.0, 0.974863),
        ],
    )
    def test_cdf_reference_values(self, t: float, df: float, expected: float) -> None:
        assert math.isclose(stats.students_t_cdf(t, df), expected, abs_tol=1e-6)

    @pytest.mark.parametrize(("t", "df"), [(0.7, 4.0), (2.3, 17.0), (5.0, 2.0)])
    def test_cdf_symmetry(self, t: float, df: float) -> None:
        total = stats.students_t_cdf(t, df) + stats.students_t_cdf(-t, df)
        assert math.isclose(total, 1.0, abs_tol=1e-12)

    @pytest.mark.parametrize(
        ("p", "df", "expected"),
        [(0.975, 10.0, 2.2281), (0.975, 3.0, 3.1824), (0.975, 128.0, 1.9787)],
    )
    def test_ppf_reference_values(self, p: float, df: float, expected: float) -> None:
        assert math.isclose(stats.students_t_ppf(p, df), expected, abs_tol=1e-3)


def test_format_p_bands() -> None:
    assert (
        stats.format_p(0.0004),
        stats.format_p(0.004),
        stats.format_p(0.124),
    ) == ("p<.001", "p=.004", "p=.124")


class TestTrendTest:
    def _points(self) -> list[tuple[date, float]]:
        ys = [2.0, 4.0, 5.0, 4.0, 5.0]
        return [(date(2026, 1, 1 + i), y) for i, y in enumerate(ys)]

    def test_reference_dataset(self) -> None:
        test = stats.trend_test(self._points())
        assert test is not None
        assert math.isclose(test.slope_per_day, 0.6, abs_tol=1e-12)
        assert math.isclose(test.per_30_days, 18.0, abs_tol=1e-9)
        assert math.isclose(test.se_per_day, 0.282843, abs_tol=1e-6)
        assert math.isclose(test.p, 0.124027, abs_tol=1e-5)
        assert math.isclose(test.ci_low_per_day, -0.300132, abs_tol=1e-4)
        assert math.isclose(test.ci_high_per_day, 1.500132, abs_tol=1e-4)
        assert test.n == 5

    def test_degenerate_inputs_return_none(self) -> None:
        two = [(date(2026, 1, 1), 1.0), (date(2026, 1, 2), 2.0)]
        same_day = [(date(2026, 1, 1), float(v)) for v in range(5)]
        collinear = [(date(2026, 1, 1 + i), 2.0 * i) for i in range(5)]
        assert (
            stats.trend_test(two),
            stats.trend_test(same_day),
            stats.trend_test(collinear),
        ) == (None, None, None)


class TestCorrTest:
    def test_fisher_ci_reference_values(self) -> None:
        low, high = stats._fisher_ci(0.5, 100)
        assert (round(low, 4), round(high, 4)) == (0.3366, 0.6341)
        low2, high2 = stats._fisher_ci(0.2, 50)
        assert (round(low2, 4), round(high2, 4)) == (-0.083, 0.4531)

    def test_corr_p_reference_values(self) -> None:
        assert math.isclose(stats._corr_p(0.5, 100), 1.180e-07, rel_tol=1e-2)
        assert math.isclose(stats._corr_p(0.2, 50), 0.16375, abs_tol=1e-3)

    def test_corr_test_end_to_end(self) -> None:
        xs = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        ys = [2.1, 1.9, 3.2, 4.8, 4.1, 6.3, 6.0, 7.9, 8.5, 9.1]
        test = stats.corr_test(xs, ys)
        assert test is not None
        assert test.ci_low < test.r < test.ci_high
        assert 0 < test.p < 0.05  # strongly correlated by construction
        assert test.n == 10

    def test_degenerate_inputs_return_none(self) -> None:
        assert (
            stats.corr_test([1.0, 2.0, 3.0], [2.0, 4.0, 6.0]),  # n < 4
            stats.corr_test([1.0, 2.0, 3.0, 4.0], [5.0, 5.0, 5.0, 5.0]),  # flat
            stats.corr_test([1.0, 2.0, 3.0, 4.0], [2.0, 4.0, 6.0, 8.0]),  # |r|=1
        ) == (None, None, None)


class TestGroupTest:
    def test_welch_worked_example(self) -> None:
        a = [27.5, 21.0, 19.0, 23.6, 17.0, 17.9, 16.9, 20.1, 21.9, 22.6, 23.1]
        a += [19.6, 19.0, 21.7, 21.4]
        b = [27.1, 22.0, 20.8, 23.4, 23.4, 23.5, 25.8, 22.0, 24.8, 20.2, 21.9]
        b += [22.1, 22.9, 30.5, 24.3, 26.4, 22.4, 24.5]
        test = stats.group_test(a, b)
        assert test is not None
        assert math.isclose(test.diff, -2.957778, abs_tol=1e-5)
        assert math.isclose(test.df, 28.4261, abs_tol=1e-3)
        assert math.isclose(test.p, 0.003660, abs_tol=1e-5)
        assert math.isclose(test.ci_low, -4.8695, abs_tol=1e-3)
        assert math.isclose(test.ci_high, -1.0461, abs_tol=1e-3)
        assert math.isclose(test.hedges_g, -1.091838, abs_tol=1e-5)
        assert (test.n_a, test.n_b) == (15, 18)

    def test_small_symmetric_groups(self) -> None:
        test = stats.group_test([1.0, 2.0, 3.0, 4.0, 5.0], [3.0, 4.0, 5.0, 6.0, 7.0])
        assert test is not None
        assert (test.diff, test.df) == (-2.0, 8.0)
        assert math.isclose(test.p, 0.080516, abs_tol=1e-5)
        assert math.isclose(test.hedges_g, -1.14250, abs_tol=1e-4)

    def test_degenerate_inputs_return_none(self) -> None:
        assert (
            stats.group_test([1.0], [2.0, 3.0]),  # a too small
            stats.group_test([2.0, 2.0], [3.0, 3.0]),  # both variances zero
        ) == (None, None)


class TestBhFdr:
    def test_step_up_reference(self) -> None:
        q = stats.bh_fdr([0.005, 0.011, 0.02, 0.04, 0.13])
        assert [round(v, 5) for v in q] == [0.025, 0.0275, 0.03333, 0.05, 0.13]

    def test_order_preserved_with_monotonic_clamp(self) -> None:
        q = stats.bh_fdr([0.01, 0.04, 0.03, 0.005])
        assert [round(v, 5) for v in q] == [0.02, 0.04, 0.04, 0.02]

    def test_all_weak_and_empty(self) -> None:
        assert [round(v, 9) for v in stats.bh_fdr([0.5, 0.6, 0.7])] == [0.7, 0.7, 0.7]
        assert stats.bh_fdr([]) == []

    def test_q_never_below_p(self) -> None:
        pvals = [0.001, 0.02, 0.3, 0.7, 0.048]
        assert all(q >= p for p, q in zip(pvals, stats.bh_fdr(pvals), strict=True))
