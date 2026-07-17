"""FDR-corrected significance ranking: which analytics actually matter.

Collects every trend, correlation, and group contrast the report computes into
one test family, applies Benjamini-Hochberg across the whole family (so the
"significant" star survives multiple comparisons), and ranks the findings by a
standardized effect magnitude so trends, correlations, and contrasts can share
one table:

* trend: |slope x 30 days| / SD of the series (change per month in SD units)
* correlation: 2|r| / sqrt(1 - r^2) (the standard r -> Cohen-d conversion)
* contrast: |Hedges' g|

The raw effect (in original units) is always shown alongside, so the
standardization never hides what a finding actually says.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from runlog.analyze import stats
from runlog.analyze.style import sig_value

if TYPE_CHECKING:
    from collections.abc import Sequence

Kind = Literal["trend", "correlation", "contrast"]

_ALPHA = 0.05


@dataclass(frozen=True)
class Candidate:
    """One potential finding; exactly one of trend/corr/group should be set."""

    kind: Kind
    label: str  # "Resting HR trend", "Load -> next-day HRV"
    lane: str  # "training" | "recovery" | "lifestyle" | "cross"
    unit: str  # unit of the raw effect, e.g. "bpm/30d"
    trend: stats.TrendTest | None = None
    sd: float | None = None  # pstdev of the series (standardizes trends)
    corr: stats.CorrTest | None = None
    group: stats.GroupTest | None = None


@dataclass(frozen=True)
class Finding:
    label: str
    lane: str
    kind: Kind
    effect: str  # preformatted raw effect, e.g. "-0.31 bpm/30d"
    std_effect: float  # standardized magnitude — the ranking key
    ci: str  # "[-0.52, -0.11]" in raw units
    p: float
    q: float
    n: int
    significant: bool  # q < alpha


@dataclass(frozen=True)
class SignificanceTable:
    findings: tuple[Finding, ...]  # significant first, then |std_effect| desc
    n_tests: int
    alpha: float


def _scored(candidate: Candidate) -> tuple[str, float, str, float, int] | None:
    """(effect, std_effect, ci, p, n) for whichever test the candidate holds."""
    if candidate.kind == "trend" and candidate.trend is not None:
        test = candidate.trend
        if not candidate.sd:
            return None
        low, high = test.ci_30d
        return (
            f"{'+' if test.per_30_days >= 0 else ''}"
            f"{sig_value(test.per_30_days)} {candidate.unit}",
            abs(test.per_30_days) / candidate.sd,
            f"[{sig_value(low)}, {sig_value(high)}]",
            test.p,
            test.n,
        )
    if candidate.kind == "correlation" and candidate.corr is not None:
        corr = candidate.corr
        return (
            f"r={corr.r:+.2f}",
            2 * abs(corr.r) / (1 - corr.r**2) ** 0.5,
            f"[{corr.ci_low:+.2f}, {corr.ci_high:+.2f}]",
            corr.p,
            corr.n,
        )
    if candidate.kind == "contrast" and candidate.group is not None:
        group = candidate.group
        return (
            f"g={group.hedges_g:+.2f}",
            abs(group.hedges_g),
            f"[{sig_value(group.ci_low)}, {sig_value(group.ci_high)}]",
            group.p,
            group.n_a + group.n_b,
        )
    return None


def build_table(
    candidates: Sequence[Candidate], alpha: float = _ALPHA
) -> SignificanceTable:
    """Score the family, BH-correct it once, and rank the findings."""
    scored = [
        (candidate, values)
        for candidate in candidates
        if (values := _scored(candidate)) is not None
    ]
    qvals = stats.bh_fdr([values[3] for _, values in scored])
    findings = [
        Finding(
            label=candidate.label,
            lane=candidate.lane,
            kind=candidate.kind,
            effect=effect,
            std_effect=std_effect,
            ci=ci,
            p=p,
            q=q,
            n=n,
            significant=q < alpha,
        )
        for (candidate, (effect, std_effect, ci, p, n)), q in zip(
            scored, qvals, strict=True
        )
    ]
    findings.sort(key=lambda f: (not f.significant, -f.std_effect))
    return SignificanceTable(
        findings=tuple(findings), n_tests=len(findings), alpha=alpha
    )
