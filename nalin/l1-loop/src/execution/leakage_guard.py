"""No-lookahead runtime enforcement (Task 2.1)."""

from __future__ import annotations

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
