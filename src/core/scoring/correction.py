"""Multiple-testing correction over a campaign's trial ledger.

**This is the project's stated contribution and until Task 30 nothing consumed
the FAR column.** Every system in the reference bibliography accepts a change
when a single scalar improves and none corrects across hundreds of nightly
trials. In LLM pretraining that is survivable; ported to a Sharpe ratio it is a
false-discovery machine. This module is the part that was missing.

Three views, reported side by side rather than one chosen, because they answer
different questions and a committee should see the disagreement:

- **Benjamini-Hochberg** controls the false *discovery* rate -- the expected
  share of accepted candidates that have no edge. The right target when the
  output is a shortlist rather than a single claim.
- **Holm** controls the family-wise error rate -- the probability of *any* false
  accept. Stricter, and the right target if one accepted candidate is going to be
  traded.
- **Expected-max deflation** is the DSR-shaped view: what the best of N draws
  from the null looks like, so a best-of-N score can be read against the bar that
  selection alone would clear.

And beside all three, the **uncorrected best-of-N**, because that is what the
literature this project is arguing with would report, and the contrast is the
argument.

Both step procedures take p-values. The FAR column is a p-value column -- that
claim is examined at length in `docs/design/metric.md`, including where it is
weakest -- and the corrections here inherit whatever validity it has. They cannot
manufacture any.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence


@dataclass(frozen=True, slots=True)
class Decision:
    """One trial's place in the corrected ordering."""

    index: int                  # position in the campaign, not in the sorted order
    label: str
    far: float
    rank: int                   # 1-based, ascending by far
    bh_critical: float
    bh_accepted: bool
    holm_critical: float
    holm_accepted: bool

    def as_dict(self) -> dict:
        return {
            "index": self.index, "label": self.label, "far": self.far,
            "rank": self.rank,
            "bh_critical": self.bh_critical, "bh_accepted": self.bh_accepted,
            "holm_critical": self.holm_critical, "holm_accepted": self.holm_accepted,
        }


@dataclass
class CorrectionResult:
    n: int
    q: float
    decisions: list[Decision] = field(default_factory=list)
    bh_discoveries: int = 0
    holm_discoveries: int = 0
    uncorrected_discoveries: int = 0
    best_far: float | None = None
    best_label: str = ""
    expected_max_note: str = ""

    def as_dict(self) -> dict:
        return {
            "n": self.n, "q": self.q,
            "bh_discoveries": self.bh_discoveries,
            "holm_discoveries": self.holm_discoveries,
            "uncorrected_discoveries": self.uncorrected_discoveries,
            "best_far": self.best_far, "best_label": self.best_label,
            "decisions": [d.as_dict() for d in self.decisions],
            "expected_max_note": self.expected_max_note,
        }


def correct(
    fars: Sequence[float],
    *,
    q: float = 0.05,
    labels: Sequence[str] | None = None,
    uncorrected_alpha: float | None = None,
) -> CorrectionResult:
    """Benjamini-Hochberg and Holm over a campaign's FAR column.

    `n` is `len(fars)` and nothing is filtered out of it. A crashed trial is a
    trial that was run and looked at, so it enters with the worst possible
    p-value rather than being dropped -- dropping it would shrink the
    denominator by exactly the trials that did not work, which is the most
    natural way to cheat this correction and the one nobody would notice.

    `uncorrected_alpha` defaults to `q`, and the count it produces is the
    comparison, not a decision.
    """
    n = len(fars)
    alpha = q if uncorrected_alpha is None else uncorrected_alpha
    labels = list(labels) if labels is not None else [f"trial-{i}" for i in range(n)]
    if n == 0:
        return CorrectionResult(n=0, q=q)
    if not 0 < q < 1:
        raise ValueError(f"q must be in (0, 1), got {q}")

    order = sorted(range(n), key=lambda i: (fars[i], i))

    # --- Benjamini-Hochberg: find the largest rank whose p <= (k/n)q, accept
    #     everything at or below it. The step-up is why an accepted trial can
    #     have a p-value above its own critical value -- it is carried by a
    #     smaller one further down.
    bh_cut = 0
    for rank, i in enumerate(order, start=1):
        if fars[i] <= rank / n * q:
            bh_cut = rank

    # --- Holm: step down. Stop at the first rank whose p exceeds q/(n-k+1);
    #     nothing after it is accepted, whatever its own p-value.
    holm_cut = 0
    for rank, i in enumerate(order, start=1):
        if fars[i] <= q / (n - rank + 1):
            holm_cut = rank
        else:
            break

    decisions = []
    for rank, i in enumerate(order, start=1):
        decisions.append(Decision(
            index=i, label=labels[i], far=float(fars[i]), rank=rank,
            bh_critical=rank / n * q, bh_accepted=rank <= bh_cut,
            holm_critical=q / (n - rank + 1), holm_accepted=rank <= holm_cut,
        ))

    best = order[0]
    return CorrectionResult(
        n=n, q=q, decisions=decisions,
        bh_discoveries=bh_cut,
        holm_discoveries=holm_cut,
        uncorrected_discoveries=sum(1 for f in fars if f <= alpha),
        best_far=float(fars[best]), best_label=labels[best],
    )


def deflation_view(
    best_score: float, n_trials: int, n_sessions: int, *,
    metric: str = "sortino_per_session", dataset: str | None = None,
    path=None,
) -> dict:
    """The DSR-shaped read: the best score against what selection alone clears.

    `expected_max` is the bar the *null* reaches when you take the best of N
    draws from it. A best-of-N score below that bar is not evidence of anything;
    above it, the margin is what selection cannot explain.

    Reported as a margin rather than a p-value, deliberately. Turning it into one
    needs the distribution of the maximum, not just its mean, and the tabulated
    null keeps moments rather than draws -- so a p-value here would be a
    normal-approximation artefact dressed as a rate. The two step procedures
    above are where the rates come from.
    """
    from .calibration.null import expected_max

    bar = expected_max(n_trials, n_sessions, metric=metric, dataset=dataset,
                       path=path)
    return {
        "best_score": float(best_score),
        "expected_max_under_null": float(bar),
        "margin": float(best_score - bar),
        "clears_selection_bar": bool(best_score > bar),
        "n_trials": int(n_trials),
        "n_sessions": int(n_sessions),
        "note": ("a margin, not a p-value: the tabulated null keeps moments "
                 "rather than draws, so the distribution of the maximum is not "
                 "available and any rate quoted here would be an approximation "
                 "dressed as a measurement"),
    }


# --------------------------------------------------------------------------- #
# the correction has to change shape (Task 31 Part D)
# --------------------------------------------------------------------------- #
# This is arithmetic and it holds however good the null is.
#
# BH rejects the smallest p-value only if it beats `q/N`. An empirical null built
# from R draws resolves a p-value no finer than `1/(R+1)`. So a campaign can only
# produce a discovery when
#
#     1/(R+1) <= q/N        i.e.        N <= q(R+1)
#
# At q = 0.05 with R = 1,000 rotations per candidate that is **N <= 50**. Task 30
# measured 1.33 s per trial, which is 21,600 trials overnight. Four orders of
# magnitude, and no null closes it: the limit is that a p-value column cannot
# express a number smaller than its own resolution.
#
# So stop computing N p-values and correcting them. Simulate the null
# distribution of the **best-of-N score** directly: for each of B replicates draw
# one null score per trial from that trial's own null, take the maximum, and
# compare the campaign's observed best against the resulting distribution.
#
# N becomes a parameter of the simulation rather than a divisor of a p-value, and
# the resolution that matters is B -- which is free, because drawing from stored
# samples costs nothing. B = 10,000 puts the floor at 1e-4 for any N.
#
# What this controls is the **family-wise error rate**: the probability that the
# campaign produced any false discovery. That is the question the project asks --
# did the loop fool itself -- and it is the honest one to answer when the answer
# to "which candidate" is going to be "the best one".
MAX_OF_N_NOTE = (
    "Trials are positively correlated once an agent sees history, which narrows "
    "the true distribution of the maximum. Drawing each trial's null "
    "independently therefore overstates the spread, so the test is conservative "
    "in that direction rather than optimistic."
)


def max_of_n(
    null_samples: Sequence[Sequence[float]],
    observed_best: float,
    *,
    b: int = 10_000,
    seed: int = 17,
    q: float = 0.05,
    smooth: bool = False,
) -> dict:
    """The null distribution of the best-of-N, simulated from per-trial nulls.

    `null_samples[i]` is trial `i`'s own null score sample -- from
    `randomization.candidate_null`, or from a profile-indexed table, or from any
    other source that produces draws rather than a quantile grid.

    **`max_of_n` carries a measured liberal bias, and `smooth` is not the fix.**
    Plain resampling with replacement caps the simulated maximum at the largest
    draw each null happens to contain, so a finite null truncates its own upper
    tail and the test over-rejects. Measured on known normal nulls, 4,000
    campaigns per cell: +0.021 at R = 20, +0.011 at R = 50, and nothing
    distinguishable from zero past R = 150. `RESAMPLING_BIAS` carries the grid.

    `smooth=True` adds Gaussian jitter at a Silverman bandwidth, which removes
    the hard cap -- and **was measured and rejected**: at N = 25 it turns a
    +0.015 bias at R = 50 into a **-0.022 conservative** one, and -0.028 at
    R = 99 where plain resampling is already unbiased. The bandwidth widens the
    null and the maximum overshoots. A different error in the other direction,
    and it costs power. Kept, defaulted off, and documented here so it is not
    re-invented as an improvement.

    **The adopted remedy is R >= 150**, where the bias is inside measurement
    noise with no assumption about tail shape.

    A trial whose null is empty contributes nothing: it cannot be the campaign's
    best under the null, and giving it a placeholder would put a number nobody
    measured into the maximum.
    """
    import numpy as np

    usable = [np.asarray(s, dtype=float) for s in null_samples if len(s)]
    if not usable:
        return {"n_trials": 0, "b": b, "p": 1.0, "p_floor": 1.0 / (b + 1),
                "reject": False, "note": "no per-trial nulls were supplied"}

    rng = np.random.default_rng(seed)
    maxima = np.full(b, -np.inf)
    for sample in usable:
        draws = sample[rng.integers(0, sample.size, size=b)]
        if smooth and sample.size > 1:
            sd = float(sample.std(ddof=1))
            if sd > 0:
                # Silverman's rule. The jitter is what lets a resampled maximum
                # exceed the sample maximum, which is the whole defect.
                h = 0.9 * min(sd, _iqr(sample) / 1.34 or sd) * sample.size ** -0.2
                draws = draws + rng.normal(0.0, h, size=b)
        np.maximum(maxima, draws, out=maxima)

    exceed = int((maxima >= observed_best).sum())
    p = (1 + exceed) / (b + 1)
    quantiles = {str(a): float(np.quantile(maxima, 1 - a))
                 for a in (0.5, 0.2, 0.1, 0.05, 0.01)}
    return {
        "n_trials": len(usable),
        "b": b,
        "observed_best": float(observed_best),
        "p": p,
        "p_floor": 1.0 / (b + 1),
        "at_floor": p <= 1.0 / (b + 1) + 1e-12,
        "reject": p <= q,
        "q": q,
        "null_max_mean": float(maxima.mean()),
        "null_max_quantiles": quantiles,
        "note": MAX_OF_N_NOTE,
    }


def _iqr(sample) -> float:
    import numpy as np

    q75, q25 = np.percentile(sample, [75, 25])
    return float(q75 - q25)


def step_down_max(
    null_samples: Sequence[Sequence[float]],
    observed: Sequence[float],
    *,
    b: int = 10_000,
    seed: int = 17,
    q: float = 0.05,
) -> list[dict]:
    """Westfall-Young step-down: reject the best, drop it, re-simulate the max.

    For per-trial discoveries rather than "did the campaign find anything". Stops
    at the first non-rejection, like Holm, because the max distribution of the
    remaining set is only the right reference once everything above it has been
    rejected.
    """
    order = sorted(range(len(observed)), key=lambda i: -observed[i])
    remaining = list(order)
    out = []
    for i in order:
        result = max_of_n([null_samples[j] for j in remaining], observed[i],
                          b=b, seed=seed, q=q)
        result["index"] = i
        out.append(result)
        if not result["reject"]:
            break
        remaining.remove(i)
    return out


RESAMPLING_BIAS = {20: 0.021, 30: 0.016, 50: 0.011, 75: 0.002, 99: 0.006,
                   150: 0.002, 250: 0.003, 500: -0.003, 999: 0.003, 2000: -0.000}
RECOMMENDED_MIN_R = 150


def resampling_bias(r: int) -> float:
    """The liberal bias `max_of_n` carries at null-sample size `r`.

    Measured on known standard-normal nulls, 4,000 campaigns per cell (se
    0.0034), averaged over N in {10, 25, 50, 200}; the full grid is in
    `docs/reviews/task34/bias-grid.json`. Interpolated between grid points and
    clamped at the ends.
    """
    grid = sorted(RESAMPLING_BIAS)
    if r <= grid[0]:
        return RESAMPLING_BIAS[grid[0]]
    if r >= grid[-1]:
        return RESAMPLING_BIAS[grid[-1]]
    for lo, hi in zip(grid, grid[1:]):
        if lo <= r <= hi:
            t = (r - lo) / (hi - lo)
            return RESAMPLING_BIAS[lo] + t * (RESAMPLING_BIAS[hi] - RESAMPLING_BIAS[lo])
    return 0.0                                              # pragma: no cover


def resolution_cap(r: int, q: float = 0.05) -> dict:
    """What a null of `r` draws can and cannot support, in one place.

    `N <= q(R+1)` is the BH limit. The resampling bias is the other half and used
    to be invisible: a summary reported a p-value and said nothing about the
    systematic error attached to it. Both are reported now.
    """
    return {
        "r": r,
        "bh_max_n": int(q * (r + 1)),
        "p_floor": 1.0 / (r + 1),
        "resampling_bias": round(resampling_bias(r), 4),
        "meets_recommended_r": r >= RECOMMENDED_MIN_R,
        "recommended_min_r": RECOMMENDED_MIN_R,
        "note": ("max_of_n resamples each null with replacement, so a small null "
                 "truncates its own upper tail and the test is liberal by about "
                 "`resampling_bias`. Measured, not assumed: "
                 "docs/reviews/task34/bias-grid.json."),
    }


MAX_OF_M_NOTE = (
    "max_of_m is max_of_n with the campaign as the unit, and that is deliberate "
    "rather than lazy: section 4 of the pre-registration chooses it over a Holm "
    "budget because it uses the actual null distribution of the maximum, and says "
    "the L2 gate INHERITS the family-wise error rate measured for that machinery "
    "(0.060, CI [0.041, 0.088] at R = 150, Task 38) rather than needing its own "
    "calibration study. A second implementation of the same simulation would be a "
    "second thing to calibrate."
)


def max_of_m(
    winners: Sequence[dict],
    *,
    b: int = 10_000,
    seed: int = 17,
    q: float = 0.05,
) -> dict:
    """The L2 gate: the best of M campaign winners, on **validation**.

    `winners` is one entry per campaign, each a mapping with

        campaign_id        str
        validation_primary float -- the winner's Sortino on validation
        validation_null    sequence of float -- that winner's own null, on
                           validation, from `randomization.round_turn.null_sample`
                           over its validation fill log

    Returns the two pass conditions of §5 reported separately, plus the family it
    corrected over. **It does not decide promotion**; §5 is a pass rule and
    promotion beyond the MVP is a human decision taken on all of the evidence.

    ## Three properties that are easy to get wrong

    **Every campaign enters the family, including the ones that did badly.** The
    multiplicity comes from having looked M times, not from having looked M times
    and liked the answer. Dropping the weak winners would shrink the family and
    inflate the maximum's significance, which is the same defect as excluding
    crashed trials from N -- §3's "excluding the trials that failed is the
    cheapest way to shrink a denominator and the one nobody would look for".

    **Rule (a) is separate from rule (b) and applies to the candidate on trial.**
    §5 makes `Sortino > 0` a pass condition at L1 *and* L2, because §3b found a
    candidate losing 3,275 points scoring p = 0.0005 against its own null: the
    sign-flip null tests directional skill, not profitability. So a family can
    produce a small p while the best winner still lost money, and that must not
    read as a pass. Both are reported; `passes` requires both.

    **An incomplete family gets no verdict at all.** If any campaign is missing
    its validation null, M is unknown, and a correction over an unknown family
    size is not a correction -- it is a correction over the campaigns that
    happened to be ready. `verdict` is then `None` and `incomplete` names them.
    This is stricter than `max_of_n`, which silently skips a trial whose null is
    empty, and the asymmetry is intended: there, an empty null is a property of a
    candidate that never traded; here, a missing null is a gap in our own work.

    ## What this function cannot check, and who must

    §4's stopping rule -- that a human may stop L2 for any reason **that is not a
    validation result** -- is not checkable from these inputs and is not checked
    here. The protocol's defence is structural: validation is not scored during
    the run at all, and all M winners are scored at the stop, at once. This
    function is the arithmetic at that stop, not the discipline before it.
    """
    entries = list(winners)
    if not entries:
        return {"m_campaigns": 0, "b": b, "q": q, "verdict": None,
                "incomplete": [], "note": "no campaigns were supplied"}

    incomplete = [str(w.get("campaign_id")) for w in entries
                  if not len(w.get("validation_null") or ())]
    rule_a = {str(w.get("campaign_id")): float(w.get("validation_primary", 0.0)) > 0.0
              for w in entries}
    best = max(entries, key=lambda w: float(w.get("validation_primary", float("-inf"))))
    best_id = str(best.get("campaign_id"))
    observed = float(best.get("validation_primary", float("-inf")))

    out = {
        "m_campaigns": len(entries),
        "campaigns": [str(w.get("campaign_id")) for w in entries],
        "b": b,
        "q": q,
        "observed_best": observed,
        "best_campaign": best_id,
        "rule_a_sortino_positive": rule_a,
        "rule_a_pass": rule_a.get(best_id, False),
        "incomplete": incomplete,
        "note": MAX_OF_M_NOTE,
    }
    if incomplete:
        out.update(
            verdict=None, p=None, reject=None, passes=None,
            withheld=("no verdict: {} of {} campaigns have no validation null, so "
                      "M is unknown. A correction over an unknown family size is a "
                      "correction over the campaigns that happened to be ready."
                      ).format(len(incomplete), len(entries)))
        return out

    # The same simulation, on the same machinery, with campaigns as the unit --
    # see MAX_OF_M_NOTE for why that is the point rather than a shortcut.
    sim = max_of_n([w["validation_null"] for w in entries], observed,
                   b=b, seed=seed, q=q)
    out.update(
        p=sim["p"], p_floor=sim["p_floor"], at_floor=sim["at_floor"],
        reject=sim["reject"], null_max_mean=sim["null_max_mean"],
        null_max_quantiles=sim["null_max_quantiles"],
    )
    out["passes"] = bool(out["rule_a_pass"] and sim["reject"])
    out["verdict"] = "PASS" if out["passes"] else "FAIL"
    if sim["reject"] and not out["rule_a_pass"]:
        out["note"] = (
            "rule (b) rejected the null but rule (a) did not pass: the best "
            "winner's validation Sortino is not above zero. The sign-flip null "
            "tests directional skill, not profitability (section 3b), so a small "
            "p with a losing candidate is exactly the case rule (a) exists for. "
            + MAX_OF_M_NOTE)
    return out
