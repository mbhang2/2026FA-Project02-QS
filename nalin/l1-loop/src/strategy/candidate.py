from __future__ import annotations

import pandas as pd

from src.strategy.protocol import Strategy


class Momentum(Strategy):
    """High-conviction intraday trend-following with turnover reduction.

    Uses a 30-bar Exponential Moving Average (EMA30) as the trend
    center and a 30-bar rolling standard deviation of close prices
    as the noise-filter band. Entry requires a breakout beyond the
    band:
      - Long (+1.0): close > EMA30 + 5.0 * rolling_std
      - Short (-1.0): close < EMA30 - 5.0 * rolling_std
    Once in a position, holds it until the close crosses back through
    the EMA30 (state persistence / deadband). A minimum 15-bar
    holding period is enforced after each entry before allowing a
    position flip. All signals are shifted by 1 bar to prevent
    lookahead bias.
    """

    def generate_positions(self, price_history: pd.DataFrame) -> pd.Series:
        close = price_history["close"]

        # 30-bar Exponential Moving Average
        ema30 = close.ewm(span=30, adjust=False).mean()

        # 30-bar rolling standard deviation of close prices
        roll_std = close.rolling(window=30, min_periods=30).std()

        # Band thresholds with 5.0x multiplier for robustness against
        # outlier bars in 1-minute futures data
        upper_band = ema30 + 5.0 * roll_std
        lower_band = ema30 - 5.0 * roll_std

        # Shift everything by 1 bar to prevent lookahead bias
        close_shifted = close.shift(1)
        ema30_shifted = ema30.shift(1)
        upper_band_shifted = upper_band.shift(1)
        lower_band_shifted = lower_band.shift(1)

        # Raw signal: +1 above upper band, -1 below lower band, 0 within
        raw_signal = pd.Series(0.0, index=close.index)
        raw_signal.loc[close_shifted > upper_band_shifted] = 1.0
        raw_signal.loc[close_shifted < lower_band_shifted] = -1.0

        # State machine: enforce minimum 15-bar holding period
        final_positions = pd.Series(0.0, index=close.index)
        current_pos = 0.0
        bars_since_entry = 0

        for i in range(len(close)):
            sig = raw_signal.iloc[i]

            if current_pos == 0.0:
                if sig != 0.0:
                    current_pos = sig
                    bars_since_entry = 0
            else:
                bars_since_entry += 1

                # Exit when close crosses back through EMA30
                # (after minimum holding period)
                if current_pos == 1.0 and close_shifted.iloc[i] < ema30_shifted.iloc[i]:
                    if bars_since_entry >= 15:
                        current_pos = 0.0
                        bars_since_entry = 0
                elif current_pos == -1.0 and close_shifted.iloc[i] > ema30_shifted.iloc[i]:
                    if bars_since_entry >= 15:
                        current_pos = 0.0
                        bars_since_entry = 0

            final_positions.iloc[i] = current_pos

        return final_positions
