import pandas as pd
import numpy as np

c = pd.Series([1.0, 1.0, 1.0, 1.0])
h = pd.Series([1.0, 1.0, 1.0, 1.0])
l = pd.Series([1.0, 1.0, 1.0, 1.0])

highest = h.rolling(2).max()
lowest = l.rolling(2).min()
k = 100 * (c - lowest) / (highest - lowest)
d = k.rolling(3).mean()
print(k)
print(d)

