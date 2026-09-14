import pandas as pd
from src.strategy.protocol import Strategy
from candidates.hypothesis_md_learning_the_dynamics_of_t_20260911_153217_480131.train import Strategy as WorkerStrategy

s = WorkerStrategy()
if not isinstance(s, Strategy):
    print("Not a Strategy")
else:
    print("Is a Strategy")
