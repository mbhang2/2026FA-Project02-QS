import pandas as pd
prices = pd.Series([10, 11, 12, 13, 14], index=pd.date_range("2024-01-01", periods=5))
signal = pd.Series(0, index=prices.index, dtype=float)
sma_long = prices.rolling(window=3, min_periods=3).mean()
signal[sma_long.isna()] = float('nan')
print(signal)
