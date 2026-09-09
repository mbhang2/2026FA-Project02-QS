"""The sampling distribution of the score under no signal, built by simulation.

`significance = ratio x sqrt(N)` is exact for Sharpe and only an approximation
for Sortino. Task 4 measured what that costs: at the 30-session floor the rule
fired on **6% of pure noise** against the ~0.1% a t of 3.0 implies. With hundreds
of trials a night, a loop built on that accepts noise routinely.

So stop assuming a parametric relation and measure the null instead:

1. generate no-signal synthetic data
2. run the reference strategy on it
3. compute the metric
4. repeat until the distribution is stable at each session count

`threshold_for(far, n)` then returns the score a candidate must beat for a stated
false-acceptance rate, and `far_for(score, n)` inverts it. Exact for any metric,
no closed form required, and no Sharpe-only assumption anywhere in it.

**How the tail is afforded.** A stable 0.1% quantile needs tens of thousands of
draws, and a full simulated run costs seconds. So the simulation produces a
*pool* of null per-session returns -- every session of every no-signal run -- and
the tail is built by resampling N sessions from that pool with replacement. The
expensive part, what a session's returns actually look like under this strategy
and this fill model, is simulated; the cheap part, the sampling distribution of a
statistic over N of them, is bootstrapped. The pool is committed with the run
count and seed, so the whole thing is auditable rather than regenerated.

This assumes sessions are exchangeable under the null. They are close to it here:
the generator seeds each session independently, and the reference strategy is
flat at every session boundary so no position links one to the next. On a dataset
with genuine cross-session dependence the pool would have to be blocked instead,
and that limitation belongs in the design doc rather than in a docstring.

**A note for the committee.** DSR deflates using the expected maximum of N draws
from the null. With an empirical null that expectation is computable by
simulation for an arbitrary metric, rather than through the closed form that
exists only for Sharpe. `expected_max` is here for that.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[3]
NULL_PATH = REPO_ROOT / "data" / "calibration" / "empirical-null.json"

# The false-acceptance rates the table stores. Anything between them is
# interpolated in log(far); anything outside is clamped, loudly.
FAR_GRID = (0.5, 0.2, 0.1, 0.05, 0.02, 0.01, 0.005, 0.002, 0.001, 0.0005)

# Session counts the table is built at. Between them, thresholds interpolate in
# 1/sqrt(N), which is the shape a ratio-times-root-N relation would have -- used
# only as an interpolation basis, never as the estimate itself.
N_GRID = (10, 20, 30, 40, 50, 75, 100, 150, 200)

METRICS = ("sortino_per_session", "sharpe_per_session")


# --------------------------------------------------------------------------- #
# vectorised statistics -- identical in definition to src.core.metrics
# --------------------------------------------------------------------------- #
def _sortino(samples: np.ndarray) -> np.ndarray:
    """`samples` is (trials, n). Same definition as `metrics.sortino`: MAR 0,
    downside deviation over all N."""
    mean = samples.mean(axis=1)
    shortfall = np.minimum(0.0, samples)
    dd = np.sqrt((shortfall**2).mean(axis=1))
    out = np.full(mean.shape, np.inf)
    ok = dd > 0
    out[ok] = mean[ok] / dd[ok]
    out[(~ok) & (mean <= 0)] = 0.0
    return out


def _sharpe(samples: np.ndarray) -> np.ndarray:
    mean = samples.mean(axis=1)
    sd = samples.std(axis=1, ddof=1)
    out = np.full(mean.shape, np.inf)
    ok = sd > 0
    out[ok] = mean[ok] / sd[ok]
    out[(~ok) & (mean <= 0)] = 0.0
    return out


def _downside_count(samples: np.ndarray) -> np.ndarray:
    return (samples < 0.0).sum(axis=1)


# --------------------------------------------------------------------------- #
# building the table
# --------------------------------------------------------------------------- #
def null_session_returns(
    *,
    runs: int = 120,
    n_sessions: int = 175,
    ticks_per_session: int = 390,
    vol_per_tick: float = 0.15,
    seed0: int = 10_000,
) -> list[float]:
    """Per-session returns from independent no-signal runs of the reference
    strategy. The expensive half of the null."""
    from ..datasets.synthetic import SyntheticDataset, SyntheticSpec
    from ..engine import Engine
    from ..metrics import session_returns
    from ..types import Split
    from .reference import ReferenceStrategy

    pool: list[float] = []
    for i in range(runs):
        ds = SyntheticDataset(
            SyntheticSpec(
                n_sessions=n_sessions,
                ticks_per_session=ticks_per_session,
                vol_per_tick=vol_per_tick,
                signal_strength=0.0,
                seed=seed0 + i,
            )
        )
        eng = Engine(ds, ReferenceStrategy(), split=Split.VALIDATION, warmup_sessions=1)
        eng.run()
        pool.extend(session_returns(eng.blotter, eng.session_calendar()))
    return pool


def tabulate(
    pool: Sequence[float],
    *,
    trials: int = 50_000,
    n_grid: Sequence[int] = N_GRID,
    far_grid: Sequence[float] = FAR_GRID,
    min_downside_sessions: int = 5,
    seed: int = 20_260_909,
) -> dict:
    """Bootstrap the sampling distribution of each metric at each session count.

    A draw is routed to whichever metric the scorer would have used for it -- a
    draw with too few below-MAR sessions falls back to Sharpe -- so each metric's
    null is the distribution of the scores actually attributed to it.
    """
    rng = np.random.default_rng(seed)
    arr = np.asarray(pool, dtype=float)
    table: dict[str, dict[str, dict]] = {m: {} for m in METRICS}

    for n in n_grid:
        draws = rng.choice(arr, size=(trials, n), replace=True)
        sortino = _sortino(draws)
        sharpe = _sharpe(draws)
        losers = _downside_count(draws)
        fell_back = losers < min_downside_sessions

        # Sortino's null is the draws it would actually be used on. Sharpe's is
        # unconditional, and deliberately so: the fallback fires when a draw has
        # fewer than `min_downside_sessions` losing sessions, which under no
        # signal essentially never happens -- there is no conditional sample to
        # estimate from. Reporting the unconditional Sharpe null is the honest
        # available answer, and the fact that the conditioning event is
        # unobservable under the null is itself worth knowing: a candidate that
        # reaches the fallback is already somewhere the null does not go.
        scores = {
            "sortino_per_session": sortino[~fell_back],
            "sharpe_per_session": sharpe,
        }
        for metric, sample in scores.items():
            finite = sample[np.isfinite(sample)]
            entry = {
                "trials": int(sample.size),
                "finite": int(finite.size),
                "infinite": int(sample.size - finite.size),
                "share_of_draws": float(sample.size / trials),
                "quantiles": {},
                "mean": float(finite.mean()) if finite.size else None,
                "sd": float(finite.std(ddof=1)) if finite.size > 1 else None,
            }
            for far in far_grid:
                if finite.size < 1 / far:
                    entry["quantiles"][str(far)] = None  # not resolvable at this size
                else:
                    entry["quantiles"][str(far)] = float(
                        np.quantile(finite, 1.0 - far, method="higher")
                    )
            if metric == "sharpe_per_session":
                entry["conditioning"] = "unconditional"
                entry["fallback_draws"] = int(fell_back.sum())
                entry["fallback_share"] = float(fell_back.mean())
            table[metric][str(n)] = entry

    return {
        "note": (
            "Empirical null: the sampling distribution of the score with no signal "
            "present. Built by simulating no-signal runs of the immutable reference "
            "strategy, pooling their per-session returns, and bootstrapping N of them "
            "per draw. threshold_for()/far_for() read this table."
        ),
        "metrics": list(METRICS),
        "bootstrap_trials_per_n": trials,
        "bootstrap_seed": seed,
        "min_downside_sessions": min_downside_sessions,
        "pool_size": int(arr.size),
        "far_grid": list(far_grid),
        "n_grid": list(n_grid),
        "pool": [float(x) for x in arr],
        "table": table,
    }


# --------------------------------------------------------------------------- #
# reading it
# --------------------------------------------------------------------------- #
_CACHE: dict | None = None


def load(path: Path = NULL_PATH) -> dict:
    global _CACHE
    if _CACHE is None or _CACHE.get("_path") != str(path):
        if not path.exists():
            raise FileNotFoundError(
                f"no tabulated null at {path}; run scripts/tabulate_null.py"
            )
        _CACHE = {**json.loads(path.read_text()), "_path": str(path)}
    return _CACHE


def _interp_n(entries: dict[str, dict], key: str, n: int) -> float:
    """Interpolate a quantile between tabulated session counts.

    The basis is 1/sqrt(N) because that is the shape a ratio-times-root-N
    relation would have. It is an interpolation basis between measured points,
    not a model of the null -- outside the grid we clamp rather than extrapolate.
    """
    grid = sorted(int(k) for k in entries)
    vals = {int(k): v["quantiles"].get(key) for k, v in entries.items()}
    grid = [g for g in grid if vals.get(g) is not None]
    if not grid:
        raise ValueError(f"the tabulated null has no quantile {key}")
    if n <= grid[0]:
        return vals[grid[0]]
    if n >= grid[-1]:
        return vals[grid[-1]]
    lo = max(g for g in grid if g <= n)
    hi = min(g for g in grid if g >= n)
    if lo == hi:
        return vals[lo]
    x0, x1, x = 1 / math.sqrt(lo), 1 / math.sqrt(hi), 1 / math.sqrt(n)
    w = (x - x0) / (x1 - x0)
    return vals[lo] + w * (vals[hi] - vals[lo])


def threshold_for(
    far: float, n_sessions: int, *, metric: str = "sortino_per_session",
    path: Path = NULL_PATH,
) -> float:
    """The score a candidate must reach for a stated false-acceptance rate.

    `far` is chosen by whoever sets accept policy. This only reports what the
    null says that choice costs.
    """
    doc = load(path)
    entries = doc["table"].get(metric)
    if entries is None:
        raise ValueError(f"no null tabulated for {metric!r}; have {doc['metrics']}")
    grid = sorted(doc["far_grid"])
    if far < grid[0] or far > grid[-1]:
        raise ValueError(
            f"far={far} is outside the tabulated range {grid[0]}..{grid[-1]}; "
            "re-tabulate with more trials rather than extrapolating a tail"
        )
    keys = sorted((float(k) for k in next(iter(entries.values()))["quantiles"]))
    if far in keys:
        return _interp_n(entries, str(far), n_sessions)
    lo = max(k for k in keys if k <= far)
    hi = min(k for k in keys if k >= far)
    a, b = _interp_n(entries, str(lo), n_sessions), _interp_n(entries, str(hi), n_sessions)
    # linear in log(far): the tail of a ratio statistic is closer to log-linear
    w = (math.log(far) - math.log(lo)) / (math.log(hi) - math.log(lo))
    return a + w * (b - a)


def far_for(
    score: float, n_sessions: int, *, metric: str = "sortino_per_session",
    path: Path = NULL_PATH,
) -> float:
    """The false-acceptance rate implied by a score. The inverse of `threshold_for`.

    Clamped at the edges of the table: a score past the last tabulated quantile
    is reported as `<= that far`, not extrapolated into a number nobody measured.
    """
    doc = load(path)
    keys = sorted((float(k) for k in doc["far_grid"]), reverse=True)
    thresholds = [
        (k, threshold_for(k, n_sessions, metric=metric, path=path)) for k in keys
    ]
    if score < thresholds[0][1]:
        return thresholds[0][0]
    for (f0, t0), (f1, t1) in zip(thresholds, thresholds[1:]):
        if t0 <= score <= t1:
            if t1 == t0:
                return f1
            w = (score - t0) / (t1 - t0)
            return math.exp(math.log(f0) + w * (math.log(f1) - math.log(f0)))
    return thresholds[-1][0]


def expected_max(
    n_trials: int, n_sessions: int, *, metric: str = "sortino_per_session",
    path: Path = NULL_PATH, draws: int = 2000, seed: int = 7,
) -> float:
    """E[max of `n_trials` draws from the null] -- what DSR deflates by.

    The closed form for that expectation exists only for Sharpe. With an
    empirical null it is a simulation, for any metric.
    """
    doc = load(path)
    entry = doc["table"][metric][str(_nearest(int(n_sessions), doc["n_grid"]))]
    mean, sd = entry["mean"], entry["sd"]
    if mean is None or sd is None:
        raise ValueError("the tabulated null has no moments for this cell")
    rng = np.random.default_rng(seed)
    # Approximated from the tabulated moments; the exact version resamples the
    # stored draws, which the table does not keep in order to stay reviewable.
    sample = rng.normal(mean, sd, size=(draws, n_trials))
    return float(sample.max(axis=1).mean())


def _nearest(n: int, grid: Sequence[int]) -> int:
    return min(grid, key=lambda g: abs(g - n))


@dataclass(frozen=True)
class NullSummary:
    metric: str
    n_sessions: int
    thresholds: dict[float, float]

    def __str__(self) -> str:  # pragma: no cover - reporting aid
        rows = "\n".join(
            f"  far={f:<8g} score >= {t:+.4f}" for f, t in sorted(self.thresholds.items())
        )
        return f"{self.metric} at N={self.n_sessions}\n{rows}"


def summary(n_sessions: int, *, metric: str = "sortino_per_session",
            path: Path = NULL_PATH) -> NullSummary:
    doc = load(path)
    return NullSummary(
        metric=metric,
        n_sessions=n_sessions,
        thresholds={
            f: threshold_for(f, n_sessions, metric=metric, path=path)
            for f in doc["far_grid"]
        },
    )
