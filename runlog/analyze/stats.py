"""Inferential statistics core: exact Student-t, trend/correlation/group
tests, and Benjamini-Hochberg FDR.

Pure math with no domain knowledge, stdlib only. The Student-t distribution is
computed exactly via the regularized incomplete beta function (Lentz continued
fraction), never a normal approximation, so confidence intervals and p-values
are correct at every sample size the report sees (n ~ 5 to ~1000). Degrees of
freedom are floats throughout (Welch-Satterthwaite is fractional).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from runlog.analyze import analytics

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

_EPS = 3e-12
_FPMIN = 1e-300
_MAX_ITER = 300
_Z_95 = 1.959964  # two-sided 95% normal quantile (Fisher-z interval)


# --- Student-t via the regularized incomplete beta ---------------------------


def _log_beta(a: float, b: float) -> float:
    return math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta (Lentz's method)."""
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < _FPMIN:
        d = _FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, _MAX_ITER + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < _FPMIN:
            d = _FPMIN
        c = 1.0 + aa / c
        if abs(c) < _FPMIN:
            c = _FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < _FPMIN:
            d = _FPMIN
        c = 1.0 + aa / c
        if abs(c) < _FPMIN:
            c = _FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < _EPS:
            return h
    raise ValueError("incomplete beta continued fraction did not converge")


def regularized_incomplete_beta(a: float, b: float, x: float) -> float:
    """I_x(a, b), the regularized incomplete beta function."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(a * math.log(x) + b * math.log(1.0 - x) - _log_beta(a, b))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def students_t_cdf(t: float, df: float) -> float:
    """P(T <= t) for Student's t with (possibly fractional) df."""
    x = df / (df + t * t)
    tail = 0.5 * regularized_incomplete_beta(df / 2.0, 0.5, x)
    return 1.0 - tail if t >= 0 else tail


def students_t_ppf(p: float, df: float) -> float:
    """Inverse CDF by bisection; exact enough for CI half-widths."""
    if p == 0.5:
        return 0.0
    if p < 0.5:
        return -students_t_ppf(1.0 - p, df)
    low, high = 0.0, 200.0
    for _ in range(100):
        mid = (low + high) / 2.0
        if students_t_cdf(mid, df) < p:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0


def _two_sided_p(t: float, df: float) -> float:
    return 2.0 * (1.0 - students_t_cdf(abs(t), df))


def format_p(p: float) -> str:
    """Journal-style p rendering: p<.001, p=.004, p=.124."""
    if p < 0.001:
        return "p<.001"
    return f"p={p:.3f}".replace("0.", ".")


# --- Trend inference ----------------------------------------------------------


@dataclass(frozen=True)
class TrendTest:
    slope_per_day: float
    se_per_day: float
    ci_low_per_day: float
    ci_high_per_day: float
    p: float
    n: int

    @property
    def per_30_days(self) -> float:
        return self.slope_per_day * 30

    @property
    def ci_30d(self) -> tuple[float, float]:
        return self.ci_low_per_day * 30, self.ci_high_per_day * 30


def trend_test(points: Sequence[tuple[date, float]]) -> TrendTest | None:
    """OLS slope with exact-t 95% CI and two-sided p; None when degenerate."""
    n = len(points)
    if n < 3:
        return None
    trend = analytics.linear_trend(points)
    if trend is None:
        return None
    base = points[0][0].toordinal()
    xs = [p[0].toordinal() - base for p in points]
    mean_x = sum(xs) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx == 0:
        return None
    sse = sum(
        (y - fit) ** 2 for (_, y), (_, fit) in zip(points, trend.fitted, strict=True)
    )
    df = n - 2
    se = math.sqrt(sse / df / sxx)
    if se == 0:
        return None
    t_crit = students_t_ppf(0.975, df)
    return TrendTest(
        slope_per_day=trend.slope_per_day,
        se_per_day=se,
        ci_low_per_day=trend.slope_per_day - t_crit * se,
        ci_high_per_day=trend.slope_per_day + t_crit * se,
        p=_two_sided_p(trend.slope_per_day / se, df),
        n=n,
    )


# --- Correlation inference ----------------------------------------------------


@dataclass(frozen=True)
class CorrTest:
    r: float
    ci_low: float
    ci_high: float
    p: float
    n: int


def _fisher_ci(r: float, n: int) -> tuple[float, float]:
    z = math.atanh(r)
    se = 1.0 / math.sqrt(n - 3)
    return math.tanh(z - _Z_95 * se), math.tanh(z + _Z_95 * se)


def _corr_p(r: float, n: int) -> float:
    t = r * math.sqrt(n - 2) / math.sqrt(1.0 - r * r)
    return _two_sided_p(t, n - 2)


def corr_test(xs: Sequence[float], ys: Sequence[float]) -> CorrTest | None:
    """Pearson r with Fisher-z 95% CI and exact-t p; None when degenerate."""
    n = len(xs)
    if n < 4:
        return None
    r = analytics.pearson(xs, ys)
    if r is None or abs(r) >= 1.0:
        return None
    low, high = _fisher_ci(r, n)
    return CorrTest(r=r, ci_low=low, ci_high=high, p=_corr_p(r, n), n=n)


# --- Group contrast (Welch + Hedges' g) ----------------------------------------


@dataclass(frozen=True)
class GroupTest:
    mean_a: float
    mean_b: float
    diff: float  # mean_a - mean_b
    ci_low: float  # Welch 95% CI on the difference
    ci_high: float
    p: float  # Welch two-sided
    df: float  # Welch-Satterthwaite (fractional)
    hedges_g: float  # bias-corrected standardized difference
    n_a: int
    n_b: int


def group_test(a: Sequence[float], b: Sequence[float]) -> GroupTest | None:
    """Welch's t-test with Hedges' g effect size; None when degenerate."""
    n_a, n_b = len(a), len(b)
    if min(n_a, n_b) < 2:
        return None
    mean_a = sum(a) / n_a
    mean_b = sum(b) / n_b
    var_a = sum((v - mean_a) ** 2 for v in a) / (n_a - 1)
    var_b = sum((v - mean_b) ** 2 for v in b) / (n_b - 1)
    if var_a == 0 and var_b == 0:
        return None
    diff = mean_a - mean_b
    se = math.sqrt(var_a / n_a + var_b / n_b)
    df = (var_a / n_a + var_b / n_b) ** 2 / (
        (var_a / n_a) ** 2 / (n_a - 1) + (var_b / n_b) ** 2 / (n_b - 1)
    )
    t_crit = students_t_ppf(0.975, df)
    pooled_sd = math.sqrt(((n_a - 1) * var_a + (n_b - 1) * var_b) / (n_a + n_b - 2))
    correction = 1.0 - 3.0 / (4.0 * (n_a + n_b) - 9.0)
    return GroupTest(
        mean_a=mean_a,
        mean_b=mean_b,
        diff=diff,
        ci_low=diff - t_crit * se,
        ci_high=diff + t_crit * se,
        p=_two_sided_p(diff / se, df),
        df=df,
        hedges_g=diff / pooled_sd * correction,
        n_a=n_a,
        n_b=n_b,
    )


# --- Multiple comparisons -------------------------------------------------------


def bh_fdr(pvals: Sequence[float]) -> list[float]:
    """Benjamini-Hochberg q-values (step-up), returned in input order."""
    m = len(pvals)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvals[i])
    q = [0.0] * m
    running_min = 1.0
    for rank in range(m, 0, -1):
        index = order[rank - 1]
        running_min = min(running_min, pvals[index] * m / rank)
        q[index] = running_min
    return q
