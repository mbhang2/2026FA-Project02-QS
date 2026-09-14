import pandas as pd
import numpy as np

s = pd.Series([1, 2, 3], index=pd.date_range("2024-01-01", periods=3))
mask = np.array([True, False, True])
s[mask] = np.nan
print(s)
