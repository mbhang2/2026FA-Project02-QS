"""Source priors for the retrieval side: which sources have been worth reading.

Gowtam's idea (`Gowtam_hypothesis/researcher/bwis.py`, `origin/Gowtam/HypInt`),
rebuilt. His framing is right and worth keeping: a retrieval pipeline should
carry a prior about where good questions come from, that prior should be
updated by evidence, and it must stay **advisory** -- it ranks which question
to ask, never what the gate decides.

Three things are different here, and each is a defect in the original that the
numbers made visible.

## 1. Concentration comes from the observed count

His `_beta_from_mean(mean, n=10.0)` set `alpha + beta = 10` for every cell,
whatever the count behind it, so a class backed by 284 papers and one backed
by 3 got identical confidence. The sample size is the whole reason to use a
Beta rather than a number, and fixing the concentration discards it.

Here a cell's posterior is `Beta(hits + 1/2, misses + 1/2)` -- Jeffreys, so a
cell with no observations is genuinely uninformative rather than confidently
average, and `concentration` reports what it rests on.

## 2. The variance is used

His `SourcePrior.variance` existed, was unit-tested, and was read by nothing:
`hypothesis_score` averaged the means. A mean with no weight on its precision
treats one vague source as equal to eight tight ones.

`pool()` is inverse-variance weighted, which is the standard way to combine
estimates of differing precision, and it returns the pooled *distribution* so
the uncertainty survives into the band.

## 3. The band accounts for uncertainty

His `bwis_band` thresholded a point estimate at 0.65 and 0.50. A score of 0.66
from a single wide source and one from forty tight ones both read "high".

`band()` requires the pooled credible interval to clear the global rate before
it will say `high`, and to fall below it before it will say `low`. A source set
too thin to distinguish from the corpus average reads `mid`, which is the
honest answer.

## What it is calibrated against

Our own registry, not an uncommitted file. 532 screened papers, each already
carrying a `feasibility` verdict from a pass that has been paid for:

    source        n   usable    rate
    arxiv       284       36   0.127
    crossref     95       25   0.263
    openalex    153       77   0.503
    ALL         532      138   0.259

"Usable" is `feasibility in {direct, adaptable}` -- the paper could be turned
into a question about this market with this data. That is the thing retrieval
is for, it is already labelled for the whole corpus, and it needs no gateway
call to recompute.

## What it must never do

**It ranks papers. It does not reach the proposer and it does not touch the
gate.** A source weight influences which question gets asked next, which §4
permits to depend on train results. It is not shown to the strategy-writing
agent, it is not a term in any score, and `band()` -- not the float -- is what
any outer loop may read, so a loop cannot optimise against the calibration.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from .._roots import repo_root

REPO_ROOT = repo_root()
CONFIG_PATH = REPO_ROOT / "config" / "bwis-priors.json"

#: Jeffreys. A cell with nothing in it is uninformative, not average.
PRIOR_HITS = 0.5
PRIOR_MISSES = 0.5

#: `feasibility` verdicts that count as "this paper was worth reading".
USABLE = frozenset({"direct", "adaptable"})

#: Credible mass the band uses. 0.80 rather than 0.95 because this is a
#: ranking aid: insisting on 95% would call almost everything `mid` at the
#: counts a per-theme cell actually has, and a ranking that never ranks is no
#: more useful than one that ranks badly.
BAND_MASS = 0.80



# --------------------------------------------------------------------------- #
# the regularised incomplete beta, and its inverse by bisection
#
# No scipy in this environment and `math` has no `betainc`, so the continued
# fraction below is the standard Lentz evaluation of I_x(a, b). It is here
# rather than approximated because the band's whole job is to be honest at low
# counts, and a normal approximation is not -- see `SourcePrior.interval`.
# --------------------------------------------------------------------------- #
_ITMAX = 200
_EPS = 3.0e-16
_FPMIN = 1.0e-300


def _betacf(a: float, b: float, x: float) -> float:
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < _FPMIN:
        d = _FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, _ITMAX + 1):
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
            break
    return h


def beta_cdf(x: float, a: float, b: float) -> float:
    """`I_x(a, b)`: the probability a Beta(a, b) draw is at most x."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
             + a * math.log(x) + b * math.log1p(-x))
    front = math.exp(lbeta)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def _beta_quantile(p: float, a: float, b: float, *, tol: float = 1e-9) -> float:
    """Inverse CDF by bisection. Monotone, so bisection cannot miss."""
    if p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if beta_cdf(mid, a, b) < p:
            lo = mid
        else:
            hi = mid
        if hi - lo < tol:
            break
    return 0.5 * (lo + hi)

@dataclass(frozen=True, slots=True)
class SourcePrior:
    """A Beta over "a paper from here turns out usable", and what it rests on."""

    alpha: float
    beta: float
    n_observed: int = 0
    label: str = ""

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def concentration(self) -> float:
        """`alpha + beta`. **The number his construction threw away.**"""
        return self.alpha + self.beta

    @property
    def variance(self) -> float:
        a, b, n = self.alpha, self.beta, self.alpha + self.beta
        return (a * b) / (n * n * (n + 1.0))

    @property
    def sd(self) -> float:
        return math.sqrt(self.variance)

    def interval(self, mass: float = BAND_MASS) -> tuple[float, float]:
        """An EXACT central credible interval, by inverting the Beta CDF.

        **A normal approximation is wrong in the direction that matters, and
        the error grows exactly where the evidence is thinnest.** Measured:

            cell            mean      normal 80%        exact 80%
            2 hits of 3     0.625   [0.347, 0.903]   [0.315, 0.898]
            1 hit of 1      0.750   [0.429, 1.071]   [0.351, 0.994]
            empty cell      0.500   [0.047, 0.953]   [0.024, 0.976]
            openalex (153)  0.503   [0.452, 0.555]   [0.452, 0.555]

        At a concentration of 154 the two agree to three places, so the
        approximation would have been fine for the source cells. At a
        concentration of 2 it overstates the lower bound by 0.08 and puts the
        upper bound at 1.071 -- outside the support, needing a clamp that
        hides the error rather than reporting it. Since the band's whole job
        is to be honest about thin cells, the approximation fails precisely
        where it is needed.
        """
        tail = (1.0 - mass) / 2.0
        return (_beta_quantile(tail, self.alpha, self.beta),
                _beta_quantile(1.0 - tail, self.alpha, self.beta))

    def update(self, usable: bool, weight: float = 1.0) -> "SourcePrior":
        """One more observation. Conjugate, so this is just addition."""
        return SourcePrior(
            alpha=self.alpha + (weight if usable else 0.0),
            beta=self.beta + (0.0 if usable else weight),
            n_observed=self.n_observed + 1,
            label=self.label,
        )

    def as_dict(self) -> dict:
        return {"alpha": round(self.alpha, 4), "beta": round(self.beta, 4),
                "n_observed": self.n_observed, "mean": round(self.mean, 4),
                "concentration": round(self.concentration, 2)}


def from_counts(hits: int, misses: int, *, label: str = "") -> SourcePrior:
    """The posterior for a cell with this many observed outcomes."""
    return SourcePrior(alpha=hits + PRIOR_HITS, beta=misses + PRIOR_MISSES,
                       n_observed=hits + misses, label=label)


UNINFORMATIVE = from_counts(0, 0, label="uninformative")


def pool(priors: list[SourcePrior]) -> SourcePrior:
    """Combine priors of differing precision, weighting by 1/variance.

    **This is where his version averaged the means.** Inverse-variance
    weighting is the standard pooling rule precisely because it refuses to let
    a wide estimate drag a tight one around: a cell resting on 3 papers moves
    the pooled mean about a fortieth as much as one resting on 284.

    The pooled concentration is the SUM of the inputs', so pooling two thin
    sources does not manufacture confidence that neither had.
    """
    live = [p for p in priors if p.variance > 0.0]
    if not live:
        return UNINFORMATIVE
    weights = [1.0 / p.variance for p in live]
    total = math.fsum(weights)
    mean = math.fsum(w * p.mean for w, p in zip(weights, live)) / total
    mean = min(max(mean, 1e-9), 1.0 - 1e-9)
    conc = math.fsum(p.concentration for p in live)
    return SourcePrior(alpha=mean * conc, beta=(1.0 - mean) * conc,
                       n_observed=sum(p.n_observed for p in live),
                       label="pooled")


def band(prior: SourcePrior, *, baseline: float, mass: float = BAND_MASS) -> str:
    """`high` / `mid` / `low`, with uncertainty taken into account.

    The rule is separation from the corpus average, not a fixed threshold: a
    source set is `high` only when its interval sits ENTIRELY above the
    baseline, and `low` only when it sits entirely below. Anything that
    overlaps the baseline is `mid` -- "not distinguishable from an average
    paper", which at these counts is usually the true answer and is what a
    point-estimate threshold hides.
    """
    lo, hi = prior.interval(mass)
    if lo > baseline:
        return "high"
    if hi < baseline:
        return "low"
    return "mid"


# --------------------------------------------------------------------------- #
# the committed calibration
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class Calibration:
    """Priors by cell, plus the corpus baseline the bands are measured against."""

    baseline: float
    by_source: dict[str, SourcePrior]
    global_prior: SourcePrior
    n_papers: int
    digest: str = ""

    def prior_for(self, source: str) -> SourcePrior:
        """The cell's own posterior, or the global one when the cell is unseen.

        No blending of a class prior with a theme prior at a hand-set 0.6/0.4
        as the original did -- `pool()` does that with weights the data sets.
        """
        return self.by_source.get(source) or self.global_prior

    def score(self, sources: list[str]) -> SourcePrior:
        """Pool the cells behind a set of cited sources."""
        return pool([self.prior_for(s) for s in sources]) if sources \
            else self.global_prior

    def as_dict(self) -> dict:
        return {
            "schema": "bwis-priors/2",
            "calibrated_from": "data/papers/registry.json",
            "target": "feasibility in {direct, adaptable}",
            "n_papers": self.n_papers,
            "baseline": round(self.baseline, 4),
            "global": self.global_prior.as_dict(),
            "by_source": {k: v.as_dict() for k, v in
                          sorted(self.by_source.items())},
        }


def calibrate(papers: list[dict]) -> Calibration:
    """Count outcomes per source over the screened corpus. No gateway call.

    Only papers carrying a `feasibility` verdict are counted: an unscreened
    paper is missing data, and treating it as a miss would mark every source
    down for the corpus being bigger than the screening budget.
    """
    screened = [p for p in papers if p.get("feasibility")]
    cells: dict[str, list[int]] = {}
    hits = 0
    for p in screened:
        ok = p.get("feasibility") in USABLE
        hits += ok
        cell = cells.setdefault(str(p.get("source") or "unknown"), [0, 0])
        cell[0 if ok else 1] += 1
    n = len(screened)
    return Calibration(
        baseline=(hits / n) if n else 0.0,
        by_source={k: from_counts(h, m, label=k) for k, (h, m) in cells.items()},
        global_prior=from_counts(hits, n - hits, label="global"),
        n_papers=n,
    )


def write(path: Path, cal: Calibration) -> str:
    import hashlib

    body = cal.as_dict()
    payload = json.dumps(body, sort_keys=True)
    body["digest"] = hashlib.sha256(payload.encode()).hexdigest()[:16]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n")
    return body["digest"]


def read(path: Path | None = None) -> Calibration:
    path = path or CONFIG_PATH
    body = json.loads(path.read_text())
    if body.get("schema") != "bwis-priors/2":
        raise ValueError(
            f"{path} is schema {body.get('schema')!r}; this reader wants "
            f"bwis-priors/2. Re-run scripts/bwis_calibrate.py.")

    def _p(d: dict, label: str) -> SourcePrior:
        return SourcePrior(alpha=float(d["alpha"]), beta=float(d["beta"]),
                           n_observed=int(d.get("n_observed", 0)), label=label)

    return Calibration(
        baseline=float(body["baseline"]),
        by_source={k: _p(v, k) for k, v in (body.get("by_source") or {}).items()},
        global_prior=_p(body["global"], "global"),
        n_papers=int(body["n_papers"]),
        digest=str(body.get("digest", "")),
    )
