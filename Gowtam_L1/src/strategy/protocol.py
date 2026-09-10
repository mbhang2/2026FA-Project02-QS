"""The ``Strategy`` interface every candidate strategy must satisfy.

This is the frozen, structural contract the whole L1 pipeline runs against. A
strategy receives point-in-time-safe price data up to (and including) the
current bar and returns the position to hold for the *next* bar. Because the
signature only ever hands a strategy history up to "now", it is structurally
impossible for a conforming strategy to see a future bar — the shape itself
prevents lookahead, independent of any runtime guard.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import pandas as pd


@runtime_checkable
class Strategy(Protocol):
    """A trading strategy.

    Implementations provide ``generate_positions``, which maps point-in-time
    price history to a next-bar position.
    """

    def generate_positions(self, price_history: pd.DataFrame) -> pd.Series:
        """Return next-bar positions from history up to the current bar.

        Args:
            price_history: Point-in-time-safe price data (indexed by timestamp,
                with at least ``open``/``high``/``low``/``close``/``volume``),
                containing no bar after the one currently being decided on.

        Returns:
            A ``pd.Series`` of positions, aligned to ``price_history``'s index.
            Position convention (return value semantics) is documented by each
            implementation; the protocol itself only fixes the shape.
        """
        ...


@runtime_checkable
class VectorizedStrategy(Protocol):
    """Optional fast-path interface (additive to :class:`Strategy`).

    Implementations provide ``generate_positions_vectorized``, which computes
    positions for every bar in one causal pass, so the engine can skip the
    O(n^2) per-bar ``generate_positions`` recompute at intraday/tick
    resolution. This is *purely* a performance extension: the per-bar
    ``Strategy`` path remains the correctness baseline and must stay in sync.

    Contract (non-negotiable): the returned ``pd.Series`` is index-aligned with
    ``prices``, and ``positions[t]`` must depend only on bars *strictly before*
    ``t`` — i.e. the implementation applies the standard one-bar shift
    internally (``signal.shift(1).fillna(0.0)``). This makes a correct vectorized
    implementation byte-for-byte equivalent to the per-bar path.
    """

    def generate_positions_vectorized(self, prices: pd.DataFrame) -> pd.Series:
        """Return the full position series, aligned to ``prices.index``.

        ``positions[t]`` must be computable from ``prices.iloc[:t]`` alone
        (history strictly before ``t``); a lookahead implementation is caught by
        the engine's causality probe (see ``assert_causal_vectorized``).
        """
        ...
