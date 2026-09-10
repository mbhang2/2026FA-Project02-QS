"""The simplest possible conforming ``Strategy``: constant buy-and-hold.

Exists purely to validate the rest of the pipeline before any real strategy
logic exists — the "does the wiring work at all" fixture.
"""

from __future__ import annotations

import pandas as pd

from src.strategy.protocol import Strategy


class BuyAndHold(Strategy):
    """Full constant position (1.0) from the first bar onward."""

    def generate_positions(self, price_history: pd.DataFrame) -> pd.Series:
        return pd.Series(1.0, index=price_history.index)

    def generate_positions_vectorized(self, prices: pd.DataFrame) -> pd.Series:
        # Signal is a constant 1.0; the one-bar shift makes position[0] == 0.0
        # and position[t] == 1.0 for t >= 1, exactly matching the per-bar path.
        signal = pd.Series(1.0, index=prices.index)
        return signal.shift(1).fillna(0.0)
