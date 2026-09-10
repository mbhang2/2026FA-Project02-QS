"""Vectorized strategy fast-path (PLAN_NEXT A.4): equivalence + causality guard."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.execution.leakage_guard import LookaheadViolation, assert_causal_vectorized
from src.strategy.protocol import Strategy


def _prices(n: int = 80, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    close = 100 * np.cumprod(1 + rng.normal(0.0005, 0.01, n))
    open_ = np.r_[close[0], close[:-1]] * (1 + rng.normal(0, 0.001, n))
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) * 1.001,
            "low": np.minimum(open_, close) * 0.999,
            "close": close,
            "volume": 1000.0,
        },
        index=idx,
    )


class Momentum(Strategy):
    """20-bar momentum: long when close > SMA20 and 20-bar return > 0."""

    def generate_positions(self, h: pd.DataFrame) -> pd.Series:
        c = h["close"]
        sma = c.rolling(20).mean()
        ret = c / c.shift(20) - 1
        return ((c > sma) & (ret > 0)).astype(float)

    def generate_positions_vectorized(self, prices: pd.DataFrame) -> pd.Series:
        c = prices["close"]
        sma = c.rolling(20).mean()
        ret = c / c.shift(20) - 1
        signal = ((c > sma) & (ret > 0)).astype(float)
        return signal.shift(1).fillna(0.0)


class LeakyVectorized(Strategy):
    """Deliberately reads the next bar's close (lookahead)."""

    def generate_positions(self, h: pd.DataFrame) -> pd.Series:
        return pd.Series(0.0, index=h.index)

    def generate_positions_vectorized(self, prices: pd.DataFrame) -> pd.Series:
        close = prices["close"]
        return (close.shift(-1) > close).astype(float)


def test_vectorized_equivalence_momentum():
    prices = _prices()
    strat = Momentum()
    vec = strat.generate_positions_vectorized(prices)

    # Reproduce the per-bar path (position[t] = generate_positions(prices[:t]).iloc[-1]).
    out = []
    for t in range(len(prices)):
        history = prices.iloc[:t]
        out.append(0.0 if t == 0 else float(strat.generate_positions(history).iloc[-1]))

    assert np.allclose(vec.to_numpy(dtype=float), np.asarray(out), equal_nan=True)


def test_probe_passes_on_causal_strategy():
    assert_causal_vectorized(Momentum(), _prices())


def test_probe_catches_lookahead():
    with pytest.raises(LookaheadViolation):
        assert_causal_vectorized(LeakyVectorized(), _prices())
