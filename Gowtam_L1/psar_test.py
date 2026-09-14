import pandas as pd
import numpy as np

def compute_psar(high: np.ndarray, low: np.ndarray, close: np.ndarray, af_step=0.02, af_max=0.2):
    n = len(close)
    psar = np.zeros(n)
    if n < 2:
        return psar
    
    # Initialize
    trend = 1 # 1 for up, -1 for down
    ep = high[0] # Extreme point
    af = af_step
    psar[0] = low[0]
    
    for i in range(1, n):
        psar[i] = psar[i-1] + af * (ep - psar[i-1])
        
        if trend == 1:
            if low[i] < psar[i]:
                trend = -1
                psar[i] = ep
                ep = low[i]
                af = af_step
            else:
                if high[i] > ep:
                    ep = high[i]
                    af = min(af + af_step, af_max)
                # PSAR cannot be above prior two lows
                if i > 1:
                    psar[i] = min(psar[i], low[i-1], low[i-2])
                elif i > 0:
                    psar[i] = min(psar[i], low[i-1])
        else:
            if high[i] > psar[i]:
                trend = 1
                psar[i] = ep
                ep = high[i]
                af = af_step
            else:
                if low[i] < ep:
                    ep = low[i]
                    af = min(af + af_step, af_max)
                # PSAR cannot be below prior two highs
                if i > 1:
                    psar[i] = max(psar[i], high[i-1], high[i-2])
                elif i > 0:
                    psar[i] = max(psar[i], high[i-1])
                    
    return psar

np.random.seed(42)
prices = np.cumsum(np.random.randn(100)) + 100
h = prices + 1
l = prices - 1
c = prices

psar_vals = compute_psar(h, l, c)
print(psar_vals[:10])
