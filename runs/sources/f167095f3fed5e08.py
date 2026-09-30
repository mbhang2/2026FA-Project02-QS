"""SMOKE: the fitted reference, to prove the fitted path on real NQ data.

TRANSIENT. The committed contents of this file are the momentum baseline; this
is here for one `--purpose smoke` run of `nq-train-fitted` and is restored
immediately after. Not a candidate, not a proposal -- see the module docstring
of `src/strategies/fitted_reference.py`.
"""

from src.strategies.fitted_reference import FittedReference


class Strategy(FittedReference):
    """The reference, with the interval the profile's data actually carries."""

    def __init__(self):
        super().__init__(interval="5m", stop=8.0, target=8.0, size=1)
