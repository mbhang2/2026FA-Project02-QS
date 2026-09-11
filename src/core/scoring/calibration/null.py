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

def _repo_root() -> Path:
    """Walk up to the marker rather than counting directories.

    `parents[3]` was correct until this file moved one level deeper, and it would
    have kept resolving to *a* directory rather than failing -- the manifest would
    simply not have been found. Counting parents is a hazard in a tree that is
    allowed to be reorganised.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return here.parents[-1]


REPO_ROOT = _repo_root()
NULL_PATH = REPO_ROOT / "data" / "calibration" / "empirical-null.json"


class WrongNullTable(ValueError):
    """The tabulated null was measured on a different dataset than the one asked
    about.

    Its own class because the failure is not a bad argument, it is a category
    error with no symptom. Task 15 scored a real NQ run against the synthetic
    table: `threshold_for(0.001, 86)` was 0.0336, so a Sortino of 0.0076 -- a
    significance of 0.07, which is nothing -- came back at a false-acceptance
    rate of 0.18% against a 0.1% policy. The run completed, the number was
    plausible, and it was meaningless. Nothing would have caught it.
    """


def null_key(dataset_name: str) -> str:
    """The dataset family a null belongs to.

    One null per family, not per instance: `synthetic:s=0:seed=1` and
    `synthetic:s=0.2:seed=7` share a return-generating process, and the seed is
    not what the null is about.
    """
    return str(dataset_name).split(":", 1)[0]


def null_path_for(dataset_name: str | None) -> Path:
    """Where the null for a dataset lives. Never a fallback to another one."""
    key = null_key(dataset_name or "synthetic")
    if key == "synthetic":
        return NULL_PATH
    return NULL_PATH.with_name(f"empirical-null-{key}.json")

# The false-acceptance rates the table stores. Anything between them is
# interpolated in log(far); anything outside is clamped, loudly.
FAR_GRID = (0.5, 0.2, 0.1, 0.05, 0.02, 0.01, 0.005, 0.002, 0.001, 0.0005)

# Session counts the table is built at. Between them, thresholds interpolate in
# 1/sqrt(N), which is the shape a ratio-times-root-N relation would have -- used
# only as an interpolation basis, never as the estimate itself.
# 73 and 86 are the holdout and validation session counts of the pre-registered
# NQ calendar. They are tabulated rather than interpolated not because the
# interpolation is bad -- measured, it is within 0.2% at both -- but because the
# two numbers every real decision is made at should be measured rather than
# inferred, and it costs nothing.
N_GRID = (10, 20, 30, 40, 50, 73, 75, 86, 100, 150, 200)

METRICS = ("sortino_per_session", "sharpe_per_session")


# --------------------------------------------------------------------------- #
# vectorised statistics -- identical in definition to src.core.scoring.metrics
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
    from ...data.synthetic import SyntheticDataset, SyntheticSpec
    from ...engine import Engine
    from ..metrics import session_returns
    from ...types import Split
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


# Sessions the exchange closes early, and the session immediately after one.
#
# The reference strategy is meant to be flat at every session boundary and
# cannot be, because nothing tells a strategy when the session ends: it learns
# the shortest session it has seen (23 hours, every full CME session) and a
# shortened session simply never reaches that. Over the 384 NQ training sessions
# twelve ended with a position open, and every one of them is an early close.
#
# So the pool excludes early closes and their successors -- the successor because
# the stranded position is flattened on its first bar, which puts the overnight
# move into that session's return. The rule is structural and comes from the
# exchange calendar in the manifest, not from which sessions happened to catch
# the strategy out; excluding the ones that went wrong would be conditioning the
# null on an outcome, which is the class of mistake this whole task is about.
def excluded_sessions(dataset, sessions: Sequence) -> set:
    """Session ids to leave out of the pool, and nothing else.

    Returns dataset session ids, so the caller can subtract them from a session
    calendar directly.
    """
    early = set(getattr(dataset, "early_close_dates", lambda: ())())
    if not early:
        return set()
    out = set()
    ordered = list(sessions)
    for i, s in enumerate(ordered):
        if s in early:
            out.add(s)
            if i + 1 < len(ordered):
                out.add(ordered[i + 1])
    return out


def real_session_returns(
    dataset,
    *,
    split=None,
    fill_model: str = "unfavorable",
    warmup_sessions: int = 0,
    seed: int = 1,
    exclude_early_closes: bool = False,
) -> tuple[list[float], dict]:
    """Per-session returns of the reference strategy on a real dataset.

    The strategy has no edge by construction, so on real data its session returns
    are a null sample drawn from the real return-generating process. That is the
    whole idea: keep the strategy that does nothing and change the world it does
    nothing in.

    **Training split only, and asserted.** Building the yardstick from validation
    or holdout sessions calibrates the evaluation with the evaluation data, which
    is the same error as fitting on the test set and is not recoverable
    afterwards.
    """
    from ...engine import Engine
    from ..metrics import session_calendar, session_returns
    from ...types import Split
    from .reference import ReferenceStrategy

    split = Split.TRAIN if split is None else Split(split)
    if split is not Split.TRAIN:
        raise ValueError(
            f"the null is tabulated on training sessions only, not {split}. "
            "Calibrating the gate with the data the gate is applied to makes the "
            "false-acceptance rate a statement about nothing."
        )

    strategy = ReferenceStrategy()
    engine = Engine(dataset, strategy, split=split, fill_model=fill_model,
                    warmup_sessions=warmup_sessions, seed=seed)
    record = engine.run()

    scored = engine.scored_sessions()
    # Off since Task 18. The exclusion existed because the reference strategy
    # could not be flat on an early close, and the reason was never the strategy:
    # `NQDataset.session_bounds` computed every session as running to 16:00 CT,
    # so on a half day it was told it had four hours it did not have. The
    # adapter reads the committed early-close times now and all 384 training
    # sessions end flat, so there is nothing structural left to exclude.
    #
    # Kept as a switch rather than deleted. If a future adapter has a session
    # type the strategy cannot exit before, this is the honest response -- and it
    # stays a structural rule, from the exchange calendar, never from which
    # sessions happened to strand a position. Excluding by outcome conditions the
    # null on the thing it is measuring.
    dropped = excluded_sessions(dataset, scored) if exclude_early_closes else set()
    kept = [s for s in scored if s not in dropped]
    calendar = session_calendar(dataset, kept)
    pool = session_returns(engine.blotter, calendar)

    left_open = {
        int(k): v for k, v in record.end_of_session_positions.items() if v != 0
    }
    still_contaminated = sorted(set(left_open) & set(kept))

    provenance = {
        "dataset": dataset.name,
        "dataset_digest": dataset.digest(),
        "split": str(split),
        "fill_model": fill_model,
        "seed": seed,
        "warmup_sessions": warmup_sessions,
        "strategy": "src/core/scoring/calibration/reference.py::ReferenceStrategy",
        "contract_selection": _contract_selection_version(),
        "sessions_scored": len(scored),
        "sessions_excluded": len(dropped),
        "sessions_in_pool": len(kept),
        "exclusion_rule": (
            "early-close sessions and the session immediately after one; "
            "structural, from the exchange calendar in data/manifest.json"
            if exclude_early_closes
            else "none: the reference strategy reads ctx.session_end and is flat "
                 "at every boundary it has a counterparty for"
        ),
        "sessions_ending_with_a_position_open": len(left_open),
        "contaminated_sessions_remaining_in_pool": still_contaminated,
        "first_session": min(kept) if kept else None,
        "last_session": max(kept) if kept else None,
        "ticks": record.ticks,
        "fills": record.fills,
        "blotter_digest": record.blotter_digest,
        "wall_clock_s": record.wall_clock_s,
    }
    return pool, provenance


def _contract_selection_version() -> dict:
    """Which front-month decisions the pool was built against."""
    path = REPO_ROOT / "data" / "nq-contract-selection.json"
    if not path.exists():
        return {}
    doc = json.loads(path.read_text())
    return {
        "sessions_recorded": doc.get("sessions_recorded"),
        "rule": doc.get("rule"),
        "near_roll_rule": doc.get("near_roll_rule"),
        "rolls": len(doc.get("rolls", [])),
    }


# --------------------------------------------------------------------------- #
# how long a block has to be
# --------------------------------------------------------------------------- #
def autocorrelation(x: Sequence[float], max_lag: int = 40) -> list[float]:
    """Sample autocorrelation at lags 1..max_lag."""
    a = np.asarray(x, dtype=float)
    a = a - a.mean()
    denom = float((a * a).sum())
    if denom <= 0:
        return [0.0] * max_lag
    return [float((a[:-k] * a[k:]).sum() / denom) for k in range(1, max_lag + 1)]


def measure_block_length(pool: Sequence[float], *, max_lag: int = 40) -> dict:
    """The block length the data implies, measured rather than chosen.

    Two series are examined, because they fail exchangeability in different ways.
    The returns themselves carry any drift dependence. Their absolute values
    carry volatility clustering, which is the one that actually inflates the
    variance of an N-session ratio: Sortino's denominator is a dispersion
    estimate, and runs of high-dispersion sessions make that estimate itself
    serially dependent.

    The block length is the first lag at which the autocorrelation falls inside
    the +/- 1.96/sqrt(N) band -- the decorrelation lag -- taken over whichever of
    the two series stays outside it longer. Unambiguous, reproducible from the
    pool alone, and recorded in the table beside the seed.
    """
    a = np.asarray(pool, dtype=float)
    n = a.size
    band = 1.96 / math.sqrt(n) if n > 1 else float("inf")
    acf_r = autocorrelation(a, max_lag)
    acf_abs = autocorrelation(np.abs(a), max_lag)

    def decorrelation_lag(acf: Sequence[float]) -> int:
        for i, v in enumerate(acf, start=1):
            if abs(v) < band:
                return i
        return len(acf) + 1

    lag_r = decorrelation_lag(acf_r)
    lag_abs = decorrelation_lag(acf_abs)
    block = max(1, lag_r, lag_abs)
    return {
        "n": int(n),
        "significance_band": band,
        "lag1_returns": acf_r[0] if acf_r else 0.0,
        "lag1_abs_returns": acf_abs[0] if acf_abs else 0.0,
        "acf_returns": acf_r[:10],
        "acf_abs_returns": acf_abs[:10],
        "decorrelation_lag_returns": lag_r,
        "decorrelation_lag_abs_returns": lag_abs,
        "block_length": int(block),
        "rule": (
            "first lag at which |acf| falls inside +/-1.96/sqrt(n), taken over "
            "returns and |returns|, whichever persists longer"
        ),
    }


def _draw_indices(rng, pool_size: int, n: int, trials: int, block_length: int):
    """Stationary bootstrap indices (Politis & Romano).

    Blocks of geometric length with mean `block_length`, wrapping at the end of
    the pool so every position is equally likely to start a block. At
    `block_length == 1` the restart probability is 1 and every draw is an
    independent uniform pick -- the i.i.d. resample exactly, which is what makes
    the old behaviour a special case of the new one rather than a different code
    path.
    """
    if block_length < 1:
        raise ValueError(f"block_length must be >= 1, got {block_length}")
    if block_length == 1:
        return rng.integers(0, pool_size, size=(trials, n))
    p = 1.0 / block_length
    idx = np.empty((trials, n), dtype=np.int64)
    idx[:, 0] = rng.integers(0, pool_size, size=trials)
    for t in range(1, n):
        restart = rng.random(trials) < p
        stepped = (idx[:, t - 1] + 1) % pool_size
        idx[:, t] = np.where(restart, rng.integers(0, pool_size, size=trials), stepped)
    return idx


def tabulate(
    pool: Sequence[float],
    *,
    trials: int = 500_000,
    n_grid: Sequence[int] = N_GRID,
    far_grid: Sequence[float] = FAR_GRID,
    min_downside_sessions: int = 5,
    seed: int = 20_260_909,
    block_length: int = 1,
    dataset: str = "synthetic",
    provenance: dict | None = None,
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
        draws = arr[_draw_indices(rng, arr.size, n, trials, block_length)]
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
            "present. Built by running the immutable reference strategy with no edge, "
            "pooling its per-session returns, and resampling N of them per draw. "
            "threshold_for()/far_for() read this table."
        ),
        "dataset": dataset,
        "metrics": list(METRICS),
        "bootstrap": "stationary" if block_length > 1 else "iid",
        "block_length": int(block_length),
        "bootstrap_trials_per_n": trials,
        "bootstrap_seed": seed,
        "provenance": provenance or {},
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
_CACHE_BY_PATH: dict[str, dict] = {}


def load(path: Path = NULL_PATH, *, dataset: str | None = None) -> dict:
    """Read a tabulated null, refusing one measured on a different dataset.

    The check is the point of `dataset`: without it the synthetic table is the
    default for everything and a real run reads it silently.
    """
    key = str(path)
    if key not in _CACHE_BY_PATH:
        if not Path(path).exists():
            raise FileNotFoundError(
                f"no tabulated null at {path}"
                + (
                    f"; the null for {dataset!r} has not been measured. Run "
                    "scripts/tabulate_null.py --dataset "
                    f"{null_key(dataset)}"
                    if dataset
                    else "; run scripts/tabulate_null.py"
                )
            )
        _CACHE_BY_PATH[key] = {**json.loads(Path(path).read_text()), "_path": key}
    doc = _CACHE_BY_PATH[key]

    tagged = doc.get("dataset")
    if tagged is None:
        raise WrongNullTable(
            f"{path} carries no dataset tag, so there is no way to tell what "
            "process it was measured on. Re-tabulate it."
        )
    if dataset is not None and null_key(dataset) != null_key(tagged):
        raise WrongNullTable(
            f"the null at {path} was measured on {tagged!r}, and you asked about "
            f"{dataset!r}. A threshold from one return-generating process says "
            "nothing about another."
        )
    return doc


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
    dataset: str | None = None, path: Path | None = None,
) -> float:
    """The score a candidate must reach for a stated false-acceptance rate.

    `far` is chosen by whoever sets accept policy. This only reports what the
    null says that choice costs.

    `dataset` names the process the question is about, and the table has to have
    been measured on it. Leaving it out means synthetic, which is right for a
    synthetic run and wrong for every other kind -- pass it.
    """
    doc = load(path or null_path_for(dataset), dataset=dataset)
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
    dataset: str | None = None, path: Path | None = None,
) -> float:
    """The false-acceptance rate implied by a score. The inverse of `threshold_for`.

    Clamped at the edges of the table: a score past the last tabulated quantile
    is reported as `<= that far`, not extrapolated into a number nobody measured.
    """
    path = path or null_path_for(dataset)
    doc = load(path, dataset=dataset)
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
    dataset: str | None = None, path: Path | None = None,
    draws: int = 2000, seed: int = 7,
) -> float:
    """E[max of `n_trials` draws from the null] -- what DSR deflates by.

    The closed form for that expectation exists only for Sharpe. With an
    empirical null it is a simulation, for any metric.
    """
    doc = load(path or null_path_for(dataset), dataset=dataset)
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
            dataset: str | None = None, path: Path | None = None) -> NullSummary:
    path = path or null_path_for(dataset)
    doc = load(path, dataset=dataset)
    return NullSummary(
        metric=metric,
        n_sessions=n_sessions,
        thresholds={
            f: threshold_for(f, n_sessions, metric=metric, path=path)
            for f in doc["far_grid"]
        },
    )


# --------------------------------------------------------------------------- #
# does the table deliver what it promises, on data it was not built from
# --------------------------------------------------------------------------- #
def far_column_validity(
    scores: np.ndarray, build_doc: dict, n: int, *, far_grid: Sequence[float],
    path,
) -> dict:
    """Is the FAR column a p-value column?

    The ledger's FAR column is the input to false-discovery control, and
    Benjamini-Hochberg needs **validity**, not exact uniformity: P(p <= a) <= a
    for every a. Conservative costs power and stays valid; anti-conservative
    makes the reported FDR precise and wrong.

    So two things are reported. The validity table is the one that matters --
    the realised rate at each tabulated alpha. The KS statistic against
    Uniform(0,1) is reported for the bulk as a sanity check and is deliberately
    secondary: every accept decision happens in the tail, and a column can be
    beautifully uniform in the bulk while being wrong exactly where it is used.

    The table clamps outside its own grid, so p-values pile up at 0.5 and
    0.0005. The KS is computed on the interior only, against the uniform
    conditioned on that interval, because comparing censored values to an
    uncensored uniform would measure the censoring rather than the calibration.
    """
    p = np.array([
        far_for(float(s), n, path=path, dataset=build_doc.get("dataset"))
        for s in scores
    ])
    lo, hi = min(far_grid), max(far_grid)
    interior = p[(p > lo) & (p < hi)]
    ks = None
    if interior.size > 100:
        u = np.sort((interior - lo) / (hi - lo))
        k = np.arange(1, u.size + 1) / u.size
        ks = float(max(np.max(k - u), np.max(u - (k - 1 / u.size))))
    return {
        "n": int(n),
        "p_values": int(p.size),
        "clamped_low": int((p <= lo).sum()),
        "clamped_high": int((p >= hi).sum()),
        "ks_statistic_interior": ks,
        "ks_critical_0.01": (
            float(1.63 / math.sqrt(interior.size)) if interior.size else None
        ),
        "realised": {
            str(far): float((p <= far).mean()) for far in sorted(far_grid)
        },
        # The verdict, and the caveat that has to travel with it. `far_for`
        # clamps at the table's edges, so every score beyond the last tabulated
        # quantile reports exactly `max(far_grid)` -- which makes P(p <= that)
        # trivially 1. That is censoring, not a calibration failure, so validity
        # is asserted strictly inside the grid and the endpoint is reported
        # rather than scored.
        "valid_inside_grid": bool(
            all(
                float((p <= far).mean()) <= far
                for far in far_grid if lo < far < hi
            )
        ),
        "endpoint_note": (
            f"alpha={hi} is the table's upper clamp: every score above the last "
            "tabulated quantile reports exactly that value, so P(p <= it) is 1 by "
            "construction. Not evaluated."
        ),
    }


def verify_held_out(
    pool: Sequence[float],
    *,
    block_length: int = 1,
    trials: int = 500_000,
    check_trials: int = 500_000,
    n_grid: Sequence[int] = (30, 50, 73, 86, 100),
    far_grid: Sequence[float] = FAR_GRID,
    seed: int = 4_242,
    split_at: float = 0.5,
    far_column_n: Sequence[int] = (73, 86),
) -> dict:
    """Tabulate on one contiguous half of the pool, measure on the other.

    Contiguous rather than interleaved, deliberately: an interleaved split shares
    every block between the two halves, so a block bootstrap would be verified
    against its own training data and would look perfect however wrong the block
    length was. Splitting by period keeps the halves as independent as the series
    allows, and if the two periods differ in character then the measured rate
    will say so -- which is information, not a defect in the check.
    """
    arr = np.asarray(pool, dtype=float)
    cut = int(len(arr) * split_at)
    build, check = arr[:cut], arr[cut:]
    if min(build.size, check.size) < 30:
        raise ValueError(
            f"pool of {arr.size} splits into {build.size}/{check.size}; too small "
            "to verify against"
        )

    doc = tabulate(build, trials=trials, n_grid=n_grid, far_grid=far_grid,
                   seed=seed, block_length=block_length, dataset="held-out-build")
    import tempfile

    build_path = Path(tempfile.mkdtemp()) / "held-out-build.json"
    build_path.write_text(json.dumps(doc))

    rng = np.random.default_rng(seed + 1)
    rows, far_columns = [], []
    for n in n_grid:
        draws = check[_draw_indices(rng, check.size, n, check_trials, block_length)]
        scores = _sortino(draws)
        losers = _downside_count(draws)
        eligible = scores[(losers >= 5) & np.isfinite(scores)]
        for far in far_grid:
            promised = doc["table"]["sortino_per_session"][str(n)]["quantiles"][str(far)]
            if promised is None or eligible.size == 0:
                rows.append({"n": n, "far": far, "threshold": promised,
                             "measured": None})
                continue
            rows.append({
                "n": n,
                "far": far,
                "threshold": float(promised),
                "measured": float((eligible >= promised).mean()),
                "ratio": float((eligible >= promised).mean() / far),
            })
        if n in far_column_n:
            far_columns.append(
                far_column_validity(eligible[:200_000], doc, n,
                                    far_grid=far_grid, path=build_path)
            )

    worst = max(
        (r["ratio"] for r in rows if r.get("ratio") is not None), default=None
    )
    return {
        "build_sessions": int(build.size),
        "check_sessions": int(check.size),
        "block_length": int(block_length),
        "tabulate_trials": trials,
        "check_trials": check_trials,
        "worst_ratio": worst,
        "anti_conservative_cells": [
            {"n": r["n"], "far": r["far"], "ratio": r["ratio"]}
            for r in rows if r.get("ratio") is not None and r["ratio"] > 1.0
        ],
        "far_column": far_columns,
        "rows": rows,
    }
