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
