"""A deliberately broken strategy that attempts lookahead by index manipulation.

Used to prove the leakage guard trips before any position is executed. If handed
a full price series (rather than correctly-sliced history), this strategy would
act on the last bar as if it were the decision point — i.e., it would leak.
"""

from __future__ import annotations

import pandas as pd

from nalin_l1.strategy.protocol import Strategy


class LeakyStrategy(Strategy):
    """Returns a position proportional to the last bar's close vs. its own previous."""

    def generate_positions(self, price_history: pd.DataFrame) -> pd.Series:
        closes = price_history["close"]
        last = closes.iloc[-1]
        prev = closes.iloc[-2] if len(closes) > 1 else last
        signal = 1.0 if last >= prev else -1.0
        return pd.Series(signal, index=price_history.index)
