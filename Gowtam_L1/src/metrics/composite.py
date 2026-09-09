"""Composite gate score (Task 3.6)."""

from __future__ import annotations

from typing import Any

DEFAULT_WEIGHTS = {
    "sharpe": 1.0,
    "max_drawdown": 1.0,
    "stability": 1.0,
    "decay": 1.0,
}


def composite_score(
    metrics: dict[str, Any], weights: dict[str, float] | None = None
) -> float:
    """Combine core metrics into a single gate score.

    Formula: ``sharpe*1 - max_drawdown*1 + stability*1 - decay_penalty``, where
    each term is scaled by its (overridable) weight. This only *adds* a score —
    it never removes or overwrites any raw metric; the caller is responsible for
    attaching the result alongside the original dict.
    """
    w = {**DEFAULT_WEIGHTS, **(weights or {})}

    sharpe = float(metrics.get("sharpe_ratio", 0.0))
    mdd = float(metrics.get("max_drawdown", 0.0))
    stability = float(metrics.get("stability_score", 0.0))
    decayed = bool(metrics.get("decay_flag", False))

    score = (
        w["sharpe"] * sharpe
        - w["max_drawdown"] * mdd
        + w["stability"] * stability
        - (w["decay"] if decayed else 0.0)
    )
    return float(score)
