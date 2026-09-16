#!/usr/bin/env python3
"""What the dry run can resolve, computed BEFORE it runs.

    python docs/reviews/task36/resolution.py

Task 36 asks whether an agent shown its own prior scores does better than one
shown nothing. That is a claim about a trajectory, and a trajectory measured
once has no error bar -- so the resolution has to be settled first, and the
statistic has to be chosen first, in public.

**The first statistic tried was the wrong one, and this is the record of it.**
The obvious reading of "does the running maximum climb faster" is the area
under the normalised running-max curve. Simulated here at N = 100 it detects a
linear trend of 1.5 SD across the campaign **8.8% of the time** -- barely above
its own 5.8% size. It is nearly powerless, because normalising by the range
reduces the curve to "what fraction of the campaign was spent at the top",
which is dominated by where one order statistic happened to land. Reported
rather than quietly swapped: a pre-registration that only shows the statistic
that worked is not a pre-registration.

**What is used instead.**

- *T1, the trend.* Spearman rho between trial index and score, over the scored
  trials. Null: the scores in a random order, B = 10,000 permutations, one
  sided. This asks the question directly -- do later proposals score higher.
- *T2, the failure rate.* Crashed-or-blocked share in the first half against
  the second. An agent that is learning the interface should break it less
  often, and that is a learning signal the score column cannot show, because a
  crashed trial has no score.
- *T3, between arms.* Rank-sum over all scores, 100 against 100, with a
  permutation null (no scipy in the lockfile, and this needs no distributional
  assumption anyway).
- *Descriptive only:* the running-maximum curve, which is what the brief asks
  to see and which nobody should read a p-value off.

Nothing here touches the API or the engine.
"""

from __future__ import annotations

import json
import pathlib

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
N = 100
B_PERM = 10_000
REPS = 2_000
ALPHA = 0.05
RNG = np.random.default_rng(36)


# -- the statistics, exactly as the report will compute them ---------------- #

def auc_running_max(scores: np.ndarray) -> float:
    """Normalised area under the running maximum. Kept to show it is weak."""
    run = np.maximum.accumulate(scores)
    lo, hi = run[0], run[-1]
    return 0.0 if hi <= lo else float(np.mean((run - lo) / (hi - lo)))


def _rank(x: np.ndarray) -> np.ndarray:
    order = np.argsort(x, kind="mergesort")
    r = np.empty(len(x), dtype=float)
    r[order] = np.arange(1, len(x) + 1, dtype=float)
    return r


def spearman(index: np.ndarray, scores: np.ndarray) -> float:
    a, b = _rank(index), _rank(scores)
    a, b = a - a.mean(), b - b.mean()
    denom = np.sqrt((a * a).sum() * (b * b).sum())
    return 0.0 if denom == 0 else float((a * b).sum() / denom)


def perm_p_trend(scores: np.ndarray, b: int = B_PERM, rng=None) -> float:
    """One sided: are later trials better than a random ordering would give?"""
    rng = rng or RNG
    idx = np.arange(len(scores), dtype=float)
    observed = spearman(idx, scores)
    perms = np.argsort(rng.random((b, len(scores))), axis=1)
    null = np.array([spearman(idx, scores[p]) for p in perms])
    return float((1 + np.sum(null >= observed)) / (b + 1))


def perm_p_shift(a: np.ndarray, c: np.ndarray, b: int = B_PERM, rng=None) -> float:
    """Rank-sum, permutation null. One sided: is `a` shifted above `c`?"""
    rng = rng or RNG
    pooled = np.concatenate([a, c])
    observed = _rank(pooled)[: len(a)].sum()
    null = np.empty(b)
    for i in range(b):
        null[i] = _rank(rng.permutation(pooled))[: len(a)].sum()
    return float((1 + np.sum(null >= observed)) / (b + 1))


# -- what each can resolve --------------------------------------------------- #

def power_auc(slope: float, reps: int, b: int) -> float:
    hits = 0
    for _ in range(reps):
        s = RNG.normal(size=N) + slope * np.arange(N) / N
        run = np.maximum.accumulate(s)
        observed = auc_running_max(s)
        perms = np.argsort(RNG.random((b, N)), axis=1)
        null = np.array([auc_running_max(s[p]) for p in perms])
        hits += ((1 + np.sum(null <= observed)) / (b + 1)) <= ALPHA
        del run
    return hits / reps


def power_trend(slope: float, reps: int, b: int) -> float:
    hits = 0
    for _ in range(reps):
        s = RNG.normal(size=N) + slope * np.arange(N) / N
        hits += perm_p_trend(s, b=b, rng=RNG) <= ALPHA
    return hits / reps


def power_shift(delta: float, reps: int, b: int) -> float:
    hits = 0
    for _ in range(reps):
        hits += perm_p_shift(RNG.normal(size=N) + delta,
                             RNG.normal(size=N), b=b, rng=RNG) <= ALPHA
    return hits / reps


def main() -> None:
    print(f"N = {N} per arm, alpha = {ALPHA}, {REPS} replications per cell")
    print(f"se on a power estimate near 0.8: {np.sqrt(0.8 * 0.2 / REPS):.4f}")
    print("permutations inside each replication: 400 (10,000 in the report "
          "itself; 400 is enough to place the power curve)\n")

    print("REJECTED -- area under the normalised running-max curve")
    auc = {}
    for slope in (0.0, 0.5, 1.5):
        auc[slope] = power_auc(slope, REPS // 4, 400)
        print(f"    slope {slope:>4} SD   detection {auc[slope]:.3f}")
    print("    nearly flat in the effect. Not used for inference.\n")

    print("T1 -- Spearman rho(index, score), permutation null")
    trend = {}
    for slope in (0.0, 0.3, 0.5, 0.75, 1.0, 1.25):
        trend[slope] = power_trend(slope, REPS, 400)
        print(f"    slope {slope:>4} SD   detection {trend[slope]:.3f}")

    print("\nT3 -- rank-sum between arms, 100 vs 100, permutation null")
    shift = {}
    for delta in (0.0, 0.2, 0.3, 0.4, 0.5):
        shift[delta] = power_shift(delta, REPS // 2, 400)
        print(f"    shift {delta:>4} SD   detection {shift[delta]:.3f}")

    r_trend = min((s for s, p in trend.items() if p >= 0.8), default=None)
    r_shift = min((d for d, p in shift.items() if p >= 0.8), default=None)
    print("\nWHAT THE DRY RUN CAN AND CANNOT RESOLVE")
    print(f"  T1 within-arm trend   80% power at a slope of {r_trend} SD "
          "across the 100 trials")
    print(f"  T3 between-arm shift  80% power at {r_shift} SD")
    print(f"  size at zero effect sits at {trend[0.0]:.3f} and {shift[0.0]:.3f}, "
          f"against a nominal {ALPHA}")
    print("  NOT resolvable: 'arm A's best score beat arm B's'. One draw "
          "against one draw; no campaign size fixes that, only more campaigns, "
          "and they are not in this budget.")
    print("  NOT resolvable: anything about a real edge. The dataset has none "
          "by construction, so a trend here is the agent climbing noise -- "
          "which is a finding about the agent, not about the market.")

    (HERE / "resolution.json").write_text(json.dumps(
        {"n_per_arm": N, "alpha": ALPHA, "reps": REPS,
         "b_permutations_in_report": B_PERM, "b_permutations_in_this_sim": 400,
         "auc_power_rejected_statistic": {str(k): v for k, v in auc.items()},
         "t1_trend_power": {str(k): v for k, v in trend.items()},
         "t3_shift_power": {str(k): v for k, v in shift.items()},
         "resolvable_trend_slope_sd": r_trend,
         "resolvable_shift_sd": r_shift}, indent=2) + "\n")
    print(f"\nwrote {HERE / 'resolution.json'}")


if __name__ == "__main__":
    main()
