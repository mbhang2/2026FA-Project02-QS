"""The simplest possible conforming ``Strategy``: constant buy-and-hold.

Exists purely to validate the rest of the pipeline before any real strategy
logic exists — the "does the wiring work at all" fixture.
"""

from __future__ import annotations

import pandas as pd

from nalin_l1.strategy.protocol import Strategy


class BuyAndHold(Strategy):
    """Full constant position (1.0) from the first bar onward."""

    def generate_positions(self, price_history: pd.DataFrame) -> pd.Series:
        return pd.Series(1.0, index=price_history.index)
