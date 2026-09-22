"""SMOKE ONLY -- the fitted reference, proving the chain on real NQ data.

Restored to the calibration reference immediately afterwards. See
`src/strategies/fitted_reference.py` for what this is and why it is deliberately
the dumbest thing that is genuinely fitted.
"""
from src.strategies.fitted_reference import FittedReference


class Strategy(FittedReference):
    pass
