"""Track-record statistics for the auditor. Stdlib only.

PSR: Bailey & Lopez de Prado, Journal of Risk (2012).
DSR: Bailey & Lopez de Prado, Journal of Portfolio Management (2014).

Sharpe inputs to PSR/DSR are per-period (not annualized). A high PSR never
overrides a leak FAIL.
"""

from __future__ import annotations

import math
from typing import Any

EULER_MASCHERONI = 0.5772156649015329
PSR_CONFIDENCE = 0.95


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_ppf(p: float, *, lo: float = -20.0, hi: float = 20.0, steps: int = 80) -> float:
    """Standard-normal quantile via bisection on erf."""
    if p <= 0.0:
        return float("-inf")
    if p >= 1.0:
        return float("inf")
    for _ in range(steps):
        mid = 0.5 * (lo + hi)
        if _norm_cdf(mid) < p:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def moments(xs: list[float]) -> dict[str, float]:
    n = len(xs)
    if n < 3:
        return {"n": float(n), "mean": 0.0, "std": 0.0, "skew": 0.0, "kurtosis": 3.0}
    mean = sum(xs) / n
    centered = [x - mean for x in xs]
    m2 = sum(c * c for c in centered) / n
    m3 = sum(c * c * c for c in centered) / n
    m4 = sum((c * c) * (c * c) for c in centered) / n
    std = math.sqrt(m2) if m2 > 0 else 0.0
    if m2 <= 0:
        skew, kurt = 0.0, 3.0
    else:
        skew = m3 / (m2 ** 1.5)
        kurt = m4 / (m2 * m2)
    return {"n": float(n), "mean": mean, "std": std, "skew": skew, "kurtosis": kurt}


def sharpe_per_period(pnl: list[float]) -> float:
    """Sample Sharpe of the bar returns; not annualized."""
    n = len(pnl)
    if n < 2:
        return 0.0
    mean = sum(pnl) / n
    var = sum((x - mean) ** 2 for x in pnl) / (n - 1)
    std = math.sqrt(var)
    if std == 0:
        return 0.0
    return mean / std


def probabilistic_sharpe(pnl: list[float], *, sr_star: float = 0.0) -> dict[str, float]:
    """P(true per-period Sharpe > sr_star | observed path)."""
    n = len(pnl)
    sr = sharpe_per_period(pnl)
    mom = moments(pnl)
    if n < 3:
        return {"n": float(n), "sr": sr, "sr_star": sr_star, "psr": 0.5, "z": 0.0, "skew": 0.0, "kurtosis": 3.0}
    skew = mom["skew"]
    kurt = mom["kurtosis"]
    se2 = 1.0 - skew * sr + ((kurt - 1.0) / 4.0) * sr * sr
    if se2 <= 0:
        se2 = 1e-12
    z = (sr - sr_star) * math.sqrt(n - 1) / math.sqrt(se2)
    return {
        "n": float(n),
        "sr": sr,
        "sr_star": sr_star,
        "skew": skew,
        "kurtosis": kurt,
        "psr": _norm_cdf(z),
        "z": z,
    }


def expected_max_sharpe(*, n_trials: int, sr_variance: float) -> float:
    """E[max SR] under the null, Bailey & Lopez de Prado (2014)."""
    n = max(int(n_trials), 1)
    if n <= 1 or sr_variance <= 0:
        return 0.0
    sigma = math.sqrt(sr_variance)
    gamma = EULER_MASCHERONI
    z1 = _norm_ppf(1.0 - 1.0 / n)
    z2 = _norm_ppf(1.0 - 1.0 / (n * math.e))
    return sigma * ((1.0 - gamma) * z1 + gamma * z2)


def deflated_sharpe(
    pnl: list[float],
    *,
    n_trials: int,
    sr_variance: float | None = None,
) -> dict[str, float]:
    """PSR against the expected-max-Sharpe benchmark from N trials."""
    n = len(pnl)
    var = 1.0 / max(n - 1, 1) if sr_variance is None else sr_variance
    sr0 = expected_max_sharpe(n_trials=n_trials, sr_variance=var)
    row = probabilistic_sharpe(pnl, sr_star=sr0)
    return {
        **row,
        "n_trials": float(max(int(n_trials), 1)),
        "sr_variance": var,
        "dsr": row["psr"],
    }


def track_record(
    pnl: list[float],
    *,
    n_trials: int = 1,
    periods_per_year: int = 252,
) -> dict[str, Any]:
    """PSR/DSR for the evidence packet. High PSR never overrides a leak."""
    from daksh.audit_agent.engine.metrics import sharpe

    psr = probabilistic_sharpe(pnl, sr_star=0.0)
    dsr = deflated_sharpe(pnl, n_trials=n_trials)
    annual = sharpe(pnl, periods_per_year=periods_per_year)
    stats_gate = "PASS" if psr["psr"] >= PSR_CONFIDENCE else "WEAK"
    return {
        "sharpe_annual": annual,
        "sharpe_per_bar": psr["sr"],
        "n": int(psr["n"]),
        "skew": psr.get("skew", 0.0),
        "kurtosis": psr.get("kurtosis", 3.0),
        "psr": psr["psr"],
        "dsr": dsr["dsr"],
        "sr_star_dsr": dsr["sr_star"],
        "n_trials": int(dsr["n_trials"]),
        "stats_gate": stats_gate,
    }
