"""Decay-resistance check (Task 3.5)."""

from __future__ import annotations

from typing import Any

DEFAULT_DECAY_THRESHOLD = 0.5

# Finite cap for the diagnostic decay ratios. ``holdout / validation`` is
# undefined (and previously returned ``inf``) when validation Sharpe is 0.0, and
# can overflow if validation is near zero. Clamping keeps the trial log
# JSON-serializable under ``allow_nan=False``.
MAX_DECAY_RATIO = 1e3


def decay_check(
    val_metrics: dict[str, Any],
    holdout_metrics: dict[str, Any],
    threshold: float = DEFAULT_DECAY_THRESHOLD,
) -> dict:
    """Compare validation vs. holdout performance to detect alpha decay.

    Returns ``sharpe_decay_ratio`` and ``cr_decay_ratio`` (holdout / validation)
    plus a ``decay_flag`` set when holdout Sharpe falls below ``threshold`` of
    validation Sharpe. Called automatically on every run — a strategy author
    never opts in.
    """
    val_sharpe = float(val_metrics.get("sharpe_ratio", 0.0))
    hold_sharpe = float(holdout_metrics.get("sharpe_ratio", 0.0))
    val_cr = float(val_metrics.get("cumulative_return", 0.0))
    hold_cr = float(holdout_metrics.get("cumulative_return", 0.0))

    sharpe_decay_ratio = _ratio(hold_sharpe, val_sharpe)
    cr_decay_ratio = _ratio(hold_cr, val_cr)

    decay_flag = hold_sharpe < threshold * val_sharpe

    return {
        "sharpe_decay_ratio": sharpe_decay_ratio,
        "cr_decay_ratio": cr_decay_ratio,
        "decay_flag": bool(decay_flag),
    }


def _ratio(holdout: float, validation: float) -> float:
    if validation == 0.0:
        # No validation edge existed to decay. Ratio is undefined; report a
        # finite value (not inf) so it stays serializable — 0.0 when flat on
        # both sides, else the finite cap.
        return 0.0 if holdout == 0.0 else MAX_DECAY_RATIO
    return max(-MAX_DECAY_RATIO, min(MAX_DECAY_RATIO, holdout / validation))
