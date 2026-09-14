import pandas as pd
import numpy as np
import sys
sys.path.append('.')

from candidates.hypothesis_md_learning_the_dynamics_of_t_20260911_153217_480131.train import Strategy

def test_full():
    strat = Strategy()
    np.random.seed(42)
    prices = pd.Series(np.cumsum(np.random.randn(250)) + 100, index=pd.date_range('2024-01-01', periods=250))
    prices_df = pd.DataFrame({'close': prices, 'high': prices + 1, 'low': prices - 1})
    
    pos = strat.generate_positions_vectorized(prices_df)
    print("Test finished successfully.")

test_full()
