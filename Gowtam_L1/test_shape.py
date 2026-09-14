import pandas as pd
from candidates.hypothesis_md_learning_the_dynamics_of_t_20260911_153217_480131.train import Strategy

df = pd.DataFrame({
    'bid': [10, 11, 12, 11, 10],
    'ask': [11, 12, 13, 12, 11]
}, index=pd.date_range("2024-01-01", periods=5))

s = Strategy()
out = s.generate_positions(df)
print(f"Input len: {len(df)}, Output len: {len(out)}")
