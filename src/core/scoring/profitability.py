"""Amendment 18: a crypto pass must show profit after costs.

The sign-flip test asks whether direction beat chance, and `Sortino > 0` asks whether
one sample ended positive. Neither asks whether expected return after costs is positive,
so control C3b passed 371 of 1,000 campaigns of a strategy that truly loses money. This
is the bound the amendment puts in place of that floor for notional profiles.

The series is the charged session-return vector the score is computed from. The
standard error is Newey-West with a Bartlett kernel and a lag fixed by the sample size,
so nothing is fitted and no random draw is involved. `alpha` is supplied by the caller,
because it depends on the selection each level has made: `q / N`, `q / M` or `q`.
"""

from __future__ import annotations

import math
from statistics import NormalDist
from typing import Sequence

import numpy as np


def applies(profile: dict, amendment_18_in_force: bool) -> bool:
    """Whether this profile is judged by the bound. Callers pass `prereg.in_force(18)`."""
    return amendment_18_in_force and profile.get("scoring_units", "points") == "notional"


def newey_west_lag(n: int) -> int:
    """`floor(4 x (T / 100)^(2/9))`, the lag Amendment 18 registers."""
    if n < 1:
        raise ValueError("the lag needs at least one session")
    return math.floor(4 * (n / 100) ** (2 / 9))


def newey_west_se(returns: Sequence[float]) -> float:
    """The Newey-West standard error of the mean, with a Bartlett kernel."""
    x = np.asarray(returns, dtype=float)
    n = x.size
    if n < 2:
        raise ValueError("a standard error needs at least two sessions")
    e = x - x.mean()
    lag = min(newey_west_lag(n), n - 1)
    variance = float(e @ e) / n
    for k in range(1, lag + 1):
        variance += 2 * (1 - k / (lag + 1)) * float(e[k:] @ e[:-k]) / n
    return math.sqrt(max(variance, 0.0) / n)


def profitability_bound(returns: Sequence[float], alpha: float) -> dict:
    """The one-sided lower bound on mean return, and whether it is above zero.

    A zero or undefined standard error fails: it cannot show anything about the mean.
    """
    if not 0 < alpha < 1:
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")
    x = np.asarray(returns, dtype=float)
    z = NormalDist().inv_cdf(1 - alpha)
    if x.size < 2 or not np.isfinite(x).all():
        return {"n_sessions": int(x.size), "alpha": alpha, "z": z, "lag": None, "mean": None,
                "se": None, "lower_bound": None, "passes": False}
    mean = float(x.mean())
    se = newey_west_se(x)
    lower = mean - z * se
    return {"n_sessions": int(x.size), "alpha": alpha, "z": z,
            "lag": min(newey_west_lag(x.size), x.size - 1), "mean": mean, "se": se,
            "lower_bound": lower, "passes": bool(se > 0 and lower > 0)}
