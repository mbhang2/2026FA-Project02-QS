import pandas as pd
import numpy as np

prices = pd.Series(np.cumsum(np.random.randn(100)) + 100)

def compute_bb_signals(prices, lookbacks, stds=[2.0]):
    signals = {}
    for lb in lookbacks:
        for st in stds:
            sma = prices.rolling(lb).mean()
            std = prices.rolling(lb).std()
            upper = sma + st * std
            lower = sma - st * std
            
            # Trend following / Mean reversion? BB can be either.
            # Usually: Price crosses upper -> overbought (sell/mean rev) or breakout (buy/trend)
            # Typically mean reversion for bounds, or breakout. Let's provide a breakout expert:
            # > upper -> +1
            # < lower -> -1
            # or mean reversion:
            # > upper -> -1
            # < lower -> +1
            # Let's provide breakout versions.
            sig = pd.Series(0.0, index=prices.index)
            sig[prices > upper] = 1.0
            sig[prices < lower] = -1.0
            sig.iloc[:(lb - 1)] = np.nan
            signals[f"BB_{lb}_{st}"] = sig
    return signals

bb = compute_bb_signals(prices, [5, 10, 20])
print(list(bb.keys()))
