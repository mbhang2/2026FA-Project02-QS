from __future__ import annotations

import pandas as pd

from src.strategy.protocol import Strategy


class Momentum(Strategy):
    """Time-series momentum strategy (Jegadeesh & Titman, 1993)
    
    Goes long (+1.0) when the current close is above its 20-bar simple moving 
    average (SMA20) AND the trailing 20-bar return is positive. Otherwise, 
    the position is flat (0.0).
    """

    def generate_positions(self, price_history: pd.DataFrame) -> pd.Series:
        # Compute 10-bar Exponential Moving Average (EMA10) of 'close'
        ema10 = price_history["close"].ewm(span=10, adjust=False).mean()

        # Compute the trailing 5-bar percentage return
        trailing_5_bar_return = price_history["close"] / price_history["close"].shift(5) - 1

        # Shift all indicators by 1 to prevent lookahead bias
        close_shifted = price_history["close"].shift(1)
        ema10_shifted = ema10.shift(1)
        trailing_5_bar_return_shifted = trailing_5_bar_return.shift(1)

        # Initialize positions to flat (default)
        positions = pd.Series(0.0, index=price_history.index)

        # Go Long (+1.0) if close > EMA10 and 5-bar return > +0.0075 (+0.75%).
        long_condition = (close_shifted > ema10_shifted) & (trailing_5_bar_return_shifted > 0.0075)
        positions.loc[long_condition] = 1.0

        # Go Short (-1.0) if close < EMA10 and 5-bar return < -0.0025 (-0.25%).
        short_condition = (close_shifted < ema10_shifted) & (trailing_5_bar_return_shifted < -0.0025)
        positions.loc[short_condition] = -1.0

        return positions
