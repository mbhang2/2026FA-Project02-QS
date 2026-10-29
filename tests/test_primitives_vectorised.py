"""The four rewritten operators equal the callbacks they replace.

Nalin's `ts_rank`, `ts_argmax`, `ts_argmin` and `ts_decay_linear` used
`rolling().apply()` with a Python callback. Measured on 20,475 bars with the
frame rebuilt each bar -- what a streaming bridge does -- `ts_rank` cost
413 ms a bar, or 2.4 hours for a single trial. The rewrites are stride-view
reductions.

**The originals are reproduced here verbatim as the reference.** A rewrite
that is merely "close" is a silent change to what every strategy using the
operator computes, so these compare element for element, NaNs included, on
inputs built to contain the cases a callback and a vectorised reduction are
most likely to disagree on: ties, all-NaN windows, partial-NaN windows, and
windows longer than the series.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.primitives import operators as O  # noqa: E402


# --- the originals, verbatim from origin/nalin/l1-loop ------------------- #
def ref_ts_rank(series, window):
    def _rank(x):
        if np.isnan(x).any():
            return np.nan
        return float(pd.Series(x).rank(pct=True).iloc[-1])
    return series.rolling(window=window).apply(_rank, raw=True)


def ref_ts_argmax(series, window):
    def _argmax(x):
        if np.isnan(x).all():
            return np.nan
        return float(len(x) - 1 - np.nanargmax(x))
    return series.rolling(window=window).apply(_argmax, raw=True)


def ref_ts_argmin(series, window):
    def _argmin(x):
        if np.isnan(x).all():
            return np.nan
        return float(len(x) - 1 - np.nanargmin(x))
    return series.rolling(window=window).apply(_argmin, raw=True)


def ref_ts_decay_linear(series, window):
    weights = np.arange(1, window + 1, dtype=float)
    weights /= weights.sum()

    def _decay(x):
        if np.isnan(x).any():
            return np.nan
        return float(np.dot(x, weights))
    return series.rolling(window=window).apply(_decay, raw=True)


PAIRS = [
    ("ts_rank", O.ts_rank, ref_ts_rank),
    ("ts_argmax", O.ts_argmax, ref_ts_argmax),
    ("ts_argmin", O.ts_argmin, ref_ts_argmin),
    ("ts_decay_linear", O.ts_decay_linear, ref_ts_decay_linear),
]


def _series(kind: str, n: int = 240) -> pd.Series:
    rng = np.random.default_rng(11)
    if kind == "smooth":
        return pd.Series(100 + np.cumsum(rng.normal(0, 1.0, n)))
    if kind == "ties":                      # heavy ties: the rank edge case
        return pd.Series(rng.integers(0, 4, n).astype(float))
    if kind == "constant":                  # every window is one tied group
        return pd.Series(np.full(n, 7.0))
    if kind == "some_nan":
        v = 100 + np.cumsum(rng.normal(0, 1.0, n))
        v[::17] = np.nan
        return pd.Series(v)
    if kind == "nan_run":                   # an all-NaN window exists
        v = 100 + np.cumsum(rng.normal(0, 1.0, n))
        v[40:70] = np.nan
        return pd.Series(v)
    raise AssertionError(kind)


@pytest.mark.parametrize("name,new,ref", PAIRS, ids=[p[0] for p in PAIRS])
@pytest.mark.parametrize("kind", ["smooth", "ties", "constant", "some_nan",
                                  "nan_run"])
@pytest.mark.parametrize("window", [1, 2, 5, 20])
def test_the_rewrite_equals_the_callback(name, new, ref, kind, window):
    s = _series(kind)
    got, want = new(s, window), ref(s, window)
    assert got.index.equals(want.index)
    # equal_nan so a NaN in the same place counts as agreement, which is the
    # point: the edge rules have to match, not just the arithmetic.
    assert np.allclose(got.to_numpy(dtype=float), want.to_numpy(dtype=float),
                       rtol=0, atol=1e-12, equal_nan=True), (
        f"{name}({kind}, window={window}) diverged from the callback at "
        f"{np.flatnonzero(~np.isclose(got.to_numpy(dtype=float), want.to_numpy(dtype=float), equal_nan=True))[:5]}")


@pytest.mark.parametrize("name,new,ref", PAIRS, ids=[p[0] for p in PAIRS])
def test_a_window_longer_than_the_series_is_all_nan_in_both(name, new, ref):
    s = _series("smooth", n=10)
    got, want = new(s, 50), ref(s, 50)
    assert got.isna().all() and want.isna().all()


def test_the_rewrite_is_the_reason_a_bridge_is_affordable():
    """Not a benchmark with a threshold -- an ordering. If the rewrite ever
    stops being dramatically faster, it has been replaced by something that
    reintroduces the per-window callback."""
    import time

    s = _series("smooth", n=4000)
    t0 = time.perf_counter(); O.ts_rank(s, 20); fast = time.perf_counter() - t0
    t0 = time.perf_counter(); ref_ts_rank(s, 20); slow = time.perf_counter() - t0
    assert fast * 5 < slow, (
        f"vectorised ts_rank took {fast*1e3:.1f}ms against the callback's "
        f"{slow*1e3:.1f}ms; the speedup that justified the rewrite is gone")
