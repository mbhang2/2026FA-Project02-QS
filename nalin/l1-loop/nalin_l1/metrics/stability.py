"""Subperiod stability metric (Task 3.3)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from nalin_l1.metrics.returns import sharpe_ratio

# Finite bound on the stability score's magnitude. The score is the negative
# coefficient of variation of per-window Sharpes; without a cap, a per-window
# Sharpe mean near zero (or a sign flip) previously produced +/- inf, which
# corrupted ``composite_score`` and broke trial-log JSON serialization
# (``allow_nan=False``). The bound keeps the consistency term proportionate to
# the other composite inputs (Sharpe ~ O(1..20)).
MAX_STABILITY = 10.0


def subperiod_stability(daily_returns: pd.Series, n_windows: int = 4) -> dict:
    """Measure consistency of performance across time.

    Splits the return series into ``n_windows`` equal chunks, computes a per-window
    Sharpe, and returns the (bounded) negative coefficient of variation of those
    Sharpes as a stability score. Higher (closer to zero) means more consistent;
    equal Sharpes in every window score 0.0, the theoretical maximum; more negative
    means more time-inconsistent.

    The denominator is ``abs(mean)`` so the score is sign-agnostic (a consistently
    *losing* strategy is not rewarded for stability — its Sharpe term alone already
    penalizes it), and the result is clamped to ``[-MAX_STABILITY, 0]`` so it can
    never emit a non-finite value.
    """
    returns = np.asarray(daily_returns, dtype=float)
    if len(returns) == 0:
        return {"per_window_sharpe": [], "stability_score": 0.0}

    n = min(n_windows, len(returns))
    chunks = np.array_split(returns, n)
    sharpes = [sharpe_ratio(pd.Series(c)) for c in chunks]

    mean_s = float(np.mean(sharpes))
    if len(sharpes) > 1:
        std_s = float(np.std(sharpes, ddof=1))
    else:
        std_s = 0.0

    if abs(mean_s) < 1e-9:
        # Coefficient of variation is undefined at zero mean. Zero dispersion
        # (all windows identically flat) is maximally consistent => 0.0; any
        # dispersion around a zero mean signals maximal inconsistency.
        stability = 0.0 if std_s < 1e-9 else -MAX_STABILITY
    else:
        stability = -min(std_s / abs(mean_s), MAX_STABILITY)

    return {"per_window_sharpe": sharpes, "stability_score": stability}
