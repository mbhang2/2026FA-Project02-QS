"""No-lookahead runtime enforcement (Task 2.1)."""

from __future__ import annotations

import numpy as np
import pandas as pd


class LookaheadViolation(Exception):
    """Raised when a strategy is handed data past the current bar."""


def assert_no_lookahead(
    price_history: pd.DataFrame, current_bar_ts: pd.Timestamp
) -> None:
    """Raise if any row in ``price_history`` has a timestamp after ``current_bar_ts``.

    Called by the orchestrator immediately before every ``generate_positions``
    invocation, wrapping the protocol boundary with an active runtime check
    rather than relying on the interface shape alone.
    """
    current = pd.Timestamp(current_bar_ts)
    if current.tzinfo is None:
        current = current.tz_localize("UTC")
    idx = price_history.index
    if idx.tz is not None and current.tzinfo is None:
        idx = idx.tz_localize(None)
    future = idx[idx > current]
    if len(future) > 0:
        offenders = ", ".join(str(t) for t in future[:5])
        raise LookaheadViolation(
            f"lookahead detected: {len(future)} row(s) after current bar "
            f"{current}; offending timestamps: {offenders}"
        )


def assert_causal_vectorized(strategy, prices: pd.DataFrame) -> None:
    """Empirically verify a :class:`VectorizedStrategy` does not look ahead.

    A vectorized strategy is handed the *full* price series (unlike the per-bar
    ``Strategy`` path, which never receives future bars), so structural
    prevention no longer applies. This probe restores the guarantee actively:
    it perturbs every bar at or after a cutoff by a large offset and asserts
    that positions strictly before the cutoff are unchanged. Any strategy that
    reads a future bar will change an earlier position and trip the check.

    This is the vectorized analogue of ``assert_no_lookahead`` and runs once per
    strategy load (not per bar), so it stays O(n).
    """
    idx = prices.index
    if len(idx) < 4:
        return

    cutoff = idx[len(idx) // 2]
    base = strategy.generate_positions_vectorized(prices)

    perturbed = prices.copy()
    suffix = perturbed.index >= cutoff
    for col in ("open", "high", "low", "close"):
        if col in perturbed.columns:
            perturbed.loc[suffix, col] += 100.0

    modified = strategy.generate_positions_vectorized(perturbed)

    prefix = base.index < cutoff
    base_p = base[prefix].to_numpy(dtype=float)
    mod_p = modified[prefix].to_numpy(dtype=float)
    if not np.allclose(base_p, mod_p, rtol=1e-9, atol=1e-12, equal_nan=True):
        raise LookaheadViolation(
            "vectorized strategy reads future bars: positions before the "
            f"perturbation cutoff {cutoff} changed when the suffix was perturbed"
        )
