"""One session of trades as arrays, so the engine builds a `Tick` only when it steps.

The event walk visits a tiny share of a session's trades. Serving every trade as a
Python object cost more than the rest of a crypto backtest: about 638 of the smoke
run's 1,174 seconds, for 1.28 billion objects of which about 839,000 were stepped.

`SessionColumns` is an optimisation and must never change a result. Indexing it gives
the same `Tick` the dataset's `ticks()` would have served.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..types import Side, Tick


@dataclass(frozen=True, slots=True)
class SessionColumns:
    """`side` is +1 for a buyer aggressor, -1 for a seller and 0 when unknown."""

    session: int
    ts: np.ndarray
    price: np.ndarray
    size: np.ndarray
    side: np.ndarray

    def __post_init__(self) -> None:
        n = self.ts.size
        if not n or any(a.size != n for a in (self.price, self.size, self.side)):
            raise ValueError("a session's columns must be non-empty and the same length")

    def __len__(self) -> int:
        return self.ts.size

    def __getitem__(self, i: int) -> Tick:
        code = int(self.side[i])
        side = Side.BUY if code > 0 else (Side.SELL if code < 0 else None)
        return Tick(int(self.ts[i]), float(self.price[i]), int(self.size[i]), self.session,
                    side=side)
