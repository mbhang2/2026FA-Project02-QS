"""Time-series and mathematical primitives for quantitative strategy signals.

All time-series operators enforce point-in-time causality by operating strictly
over trailing windows or positive lag periods. No future bars or non-causal centered
windows are ever used.
"""

from typing import Union
import pandas as pd
import numpy as np
from src.primitives.registry import register_primitive

Numeric = Union[pd.Series, float, int]


# --- Rolling-window machinery -------------------------------------------- #
#
# **Why these four operators do not use `.rolling().apply()`.**
#
# `apply` with a Python callback runs that callback once per window. Measured
# on 20,475 bars (one NQ campaign) with the whole frame rebuilt each bar, as a
# streaming bridge must: `ts_rank` cost 413 ms a bar, which is 2.4 HOURS for
# one trial, against 0.68 ms for a vectorised operator like `ts_zscore`. A
# campaign would have looked hung rather than slow.
#
# The window matrix below is a stride view, so it allocates nothing itself;
# only the reductions over it materialise, and `_CHUNK_CELLS` bounds that.
# Semantics are unchanged and `tests/test_primitives_vectorised.py` proves it
# element-for-element against the original callbacks, NaNs and ties included.

_CHUNK_CELLS = 4_000_000


def _windows(values: np.ndarray, window: int):
    """Trailing windows as a (rows, window) stride view, newest column last."""
    from numpy.lib.stride_tricks import sliding_window_view

    return sliding_window_view(values, window)


def _rolling_reduce(series: pd.Series, window: int, reduce) -> pd.Series:
    """`reduce(window_matrix) -> one value per row`, with pandas' edge rules.

    Two rules, both inherited from `rolling(window=window).apply(...)` and both
    load-bearing:

    * the first `window - 1` positions are NaN -- `min_periods` defaults to the
      window, so a partial window has no value rather than a partial one;
    * **a window containing ANY NaN is NaN.** `min_periods` counts NON-NaN
      observations, so pandas never even calls the callback for such a window.
      This is why the `if np.isnan(x).all()` branch in the original `ts_argmax`
      and `ts_argmin` was dead code, and why a rewrite that merely reproduced
      that branch diverged -- caught by
      `tests/test_primitives_vectorised.py`, which is the whole reason the
      originals are kept there as the reference.
    """
    v = np.asarray(series.to_numpy(), dtype=float)
    n = v.size
    out = np.full(n, np.nan, dtype=float)
    if n < window or window < 1:
        return pd.Series(out, index=series.index, name=series.name)

    rows = n - window + 1
    step = max(1, _CHUNK_CELLS // max(window, 1))
    sw = _windows(v, window)
    for start in range(0, rows, step):
        block = sw[start:start + step]
        got = np.asarray(reduce(block), dtype=float)
        # min_periods = window, counted over non-NaN observations.
        got = np.where(np.isnan(block).any(axis=1), np.nan, got)
        out[window - 1 + start: window - 1 + start + block.shape[0]] = got
    return pd.Series(out, index=series.index, name=series.name)


# --- Time-Series Shift & Differences ---

@register_primitive("ts_delay")
def ts_delay(series: pd.Series, periods: int = 1) -> pd.Series:
    """Lag series by positive periods (causal: t - periods)."""
    return series.shift(periods)


@register_primitive("ts_delta")
def ts_delta(series: pd.Series, periods: int = 1) -> pd.Series:
    """Difference of series against lagged series: series(t) - series(t - periods)."""
    return series - series.shift(periods)


@register_primitive("ts_returns")
def ts_returns(series: pd.Series, periods: int = 1) -> pd.Series:
    """Fractional change over periods: (series(t) - series(t - periods)) / series(t - periods)."""
    lagged = series.shift(periods)
    lagged_safe = lagged.replace(0.0, np.nan)
    ret = (series - lagged) / lagged_safe
    return ret.replace([np.inf, -np.inf], np.nan)


# --- Rolling Moments & Statistics ---

@register_primitive("ts_mean")
def ts_mean(series: pd.Series, window: int) -> pd.Series:
    """Trailing rolling mean over window."""
    return series.rolling(window=window).mean()


@register_primitive("ts_std")
def ts_std(series: pd.Series, window: int) -> pd.Series:
    """Trailing rolling sample standard deviation over window."""
    return series.rolling(window=window).std()


@register_primitive("ts_var")
def ts_var(series: pd.Series, window: int) -> pd.Series:
    """Trailing rolling sample variance over window."""
    return series.rolling(window=window).var()


@register_primitive("ts_zscore")
def ts_zscore(series: pd.Series, window: int) -> pd.Series:
    """Trailing rolling z-score: (series - mean(series, window)) / std(series, window)."""
    mean = series.rolling(window=window).mean()
    std = series.rolling(window=window).std().replace(0.0, np.nan)
    res = (series - mean) / std
    return res.replace([np.inf, -np.inf], np.nan)


@register_primitive("ts_ema")
def ts_ema(series: pd.Series, span: int) -> pd.Series:
    """Exponential moving average with given span."""
    return series.ewm(span=span, adjust=False).mean()


@register_primitive("ts_rsi")
def ts_rsi(series: pd.Series, window: int = 14) -> pd.Series:
    """Wilder's Relative Strength Index over window."""
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -1.0 * delta.clip(upper=0.0)

    # Wilder's smoothing is an EMA with alpha = 1 / window
    avg_gain = gain.ewm(alpha=1.0 / window, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / window, adjust=False).mean()

    avg_loss_safe = avg_loss.replace(0.0, np.nan)
    rs = avg_gain / avg_loss_safe
    rsi = 100.0 - (100.0 / (1.0 + rs))

    # Boundary cases:
    # If loss == 0 and gain > 0: RSI is 100.0
    # If both gain == 0 and loss == 0: flat price, RSI is 50.0
    both_flat = (avg_gain == 0.0) & (avg_loss == 0.0)
    rsi = rsi.where(~both_flat, 50.0)
    rsi = rsi.fillna(100.0).where(avg_loss != 0.0, 100.0)
    return rsi


@register_primitive("ts_min")
def ts_min(series: pd.Series, window: int) -> pd.Series:
    """Trailing rolling minimum over window."""
    return series.rolling(window=window).min()


@register_primitive("ts_max")
def ts_max(series: pd.Series, window: int) -> pd.Series:
    """Trailing rolling maximum over window."""
    return series.rolling(window=window).max()


@register_primitive("ts_sum")
def ts_sum(series: pd.Series, window: int) -> pd.Series:
    """Trailing rolling sum over window."""
    return series.rolling(window=window).sum()


@register_primitive("ts_median")
def ts_median(series: pd.Series, window: int) -> pd.Series:
    """Trailing rolling median over window."""
    return series.rolling(window=window).median()


@register_primitive("ts_quantile")
def ts_quantile(series: pd.Series, window: int, q: float = 0.5) -> pd.Series:
    """Trailing rolling quantile over window (q in [0, 1])."""
    return series.rolling(window=window).quantile(q)


@register_primitive("ts_rank")
def ts_rank(series: pd.Series, window: int) -> pd.Series:
    """Trailing rolling percentile rank of current observation (0.0 to 1.0).

    Ties share the average rank, which is `Series.rank(pct=True)`'s default and
    what the callback this replaces produced.
    """
    def _reduce(block: np.ndarray) -> np.ndarray:
        last = block[:, -1][:, None]
        # NaN compares False, so these counts are only meaningful on rows with
        # no NaN -- which are exactly the rows `_rolling_reduce` keeps.
        less = (block < last).sum(axis=1)
        equal = (block == last).sum(axis=1)
        return (less + (equal + 1) / 2.0) / block.shape[1]

    return _rolling_reduce(series, window, _reduce)


# --- Multivariate Time-Series Operators ---

@register_primitive("ts_corr")
def ts_corr(series1: pd.Series, series2: pd.Series, window: int) -> pd.Series:
    """Trailing rolling Pearson correlation between series1 and series2."""
    return series1.rolling(window=window).corr(series2)


@register_primitive("ts_cov")
def ts_cov(series1: pd.Series, series2: pd.Series, window: int) -> pd.Series:
    """Trailing rolling covariance between series1 and series2."""
    return series1.rolling(window=window).cov(series2)


# --- Temporal Location Operators ---

@register_primitive("ts_argmax")
def ts_argmax(series: pd.Series, window: int) -> pd.Series:
    """Bars ago that the maximum occurred in trailing window (0 = current bar, window-1 = oldest)."""
    def _reduce(block: np.ndarray) -> np.ndarray:
        # Any row with a NaN is discarded by `_rolling_reduce`; the fill is
        # only here so `np.nanargmax` does not raise on it first.
        safe = np.where(np.isnan(block), 0.0, block)
        idx = np.nanargmax(safe, axis=1)
        return (block.shape[1] - 1 - idx).astype(float)

    return _rolling_reduce(series, window, _reduce)


@register_primitive("ts_argmin")
def ts_argmin(series: pd.Series, window: int) -> pd.Series:
    """Bars ago that the minimum occurred in trailing window (0 = current bar, window-1 = oldest)."""
    def _reduce(block: np.ndarray) -> np.ndarray:
        # Any row with a NaN is discarded by `_rolling_reduce`; the fill is
        # only here so `np.nanargmin` does not raise on it first.
        safe = np.where(np.isnan(block), 0.0, block)
        idx = np.nanargmin(safe, axis=1)
        return (block.shape[1] - 1 - idx).astype(float)

    return _rolling_reduce(series, window, _reduce)


# --- Decays ---

@register_primitive("ts_decay_linear")
def ts_decay_linear(series: pd.Series, window: int) -> pd.Series:
    """Linearly weighted moving average with weights [1, 2, ..., window] normalized to 1."""
    weights = np.arange(1, window + 1, dtype=float)
    weights /= weights.sum()

    def _reduce(block: np.ndarray) -> np.ndarray:
        # A NaN anywhere in the window propagates through the dot product on
        # its own, which is the callback's behaviour too.
        return block @ weights

    return _rolling_reduce(series, window, _reduce)


@register_primitive("ts_decay_exp")
def ts_decay_exp(series: pd.Series, span: int) -> pd.Series:
    """Exponential decay moving average with span."""
    return series.ewm(span=span, adjust=False).mean()


# --- Arithmetic Operators ---

@register_primitive("add")
def add(a: Numeric, b: Numeric) -> pd.Series:
    """Element-wise addition: a + b."""
    return a + b


@register_primitive("sub")
def sub(a: Numeric, b: Numeric) -> pd.Series:
    """Element-wise subtraction: a - b."""
    return a - b


@register_primitive("mul")
def mul(a: Numeric, b: Numeric) -> pd.Series:
    """Element-wise multiplication: a * b."""
    return a * b


@register_primitive("div")
def div(a: Numeric, b: Numeric) -> pd.Series:
    """Element-wise safe division: a / b. Division by zero yields NaN, not inf."""
    if isinstance(b, pd.Series):
        denom = b.replace(0.0, np.nan)
    elif isinstance(b, (int, float)):
        denom = np.nan if b == 0 else b
    else:
        denom = b
    res = a / denom
    if isinstance(res, pd.Series):
        return res.replace([np.inf, -np.inf], np.nan)
    return res


# --- Comparisons ---

@register_primitive("gt")
def gt(a: Numeric, b: Numeric) -> pd.Series:
    """Greater than comparison: 1.0 if a > b else 0.0."""
    return (a > b).astype(float)


@register_primitive("lt")
def lt(a: Numeric, b: Numeric) -> pd.Series:
    """Less than comparison: 1.0 if a < b else 0.0."""
    return (a < b).astype(float)


@register_primitive("gte")
def gte(a: Numeric, b: Numeric) -> pd.Series:
    """Greater than or equal comparison: 1.0 if a >= b else 0.0."""
    return (a >= b).astype(float)


@register_primitive("lte")
def lte(a: Numeric, b: Numeric) -> pd.Series:
    """Less than or equal comparison: 1.0 if a <= b else 0.0."""
    return (a <= b).astype(float)


@register_primitive("eq")
def eq(a: Numeric, b: Numeric) -> pd.Series:
    """Equal comparison: 1.0 if a == b else 0.0."""
    return (a == b).astype(float)


@register_primitive("neq")
def neq(a: Numeric, b: Numeric) -> pd.Series:
    """Not equal comparison: 1.0 if a != b else 0.0."""
    return (a != b).astype(float)


# --- Logical Branching ---

@register_primitive("and_op")
def and_op(a: Numeric, b: Numeric) -> pd.Series:
    """Logical AND: 1.0 if (a > 0 and b > 0) else 0.0."""
    return ((a > 0) & (b > 0)).astype(float)


@register_primitive("or_op")
def or_op(a: Numeric, b: Numeric) -> pd.Series:
    """Logical OR: 1.0 if (a > 0 or b > 0) else 0.0."""
    return ((a > 0) | (b > 0)).astype(float)


@register_primitive("not_op")
def not_op(a: Numeric) -> pd.Series:
    """Logical NOT: 1.0 if not (a > 0) else 0.0."""
    return (~(a > 0)).astype(float)


@register_primitive("ts_where")
def ts_where(condition: pd.Series, true_val: Numeric, false_val: Numeric) -> pd.Series:
    """Conditional multiplexer: true_val where condition > 0 else false_val."""
    return pd.Series(np.where(condition > 0, true_val, false_val), index=condition.index)


# --- Non-Linear Math Operators ---

@register_primitive("log")
def log_op(series: pd.Series) -> pd.Series:
    """Natural log of positive values (values <= 0 yield NaN)."""
    safe_series = series.where(series > 0, np.nan)
    return np.log(safe_series)


@register_primitive("abs")
def abs_op(series: pd.Series) -> pd.Series:
    """Absolute value."""
    return series.abs()


@register_primitive("sign")
def sign_op(series: pd.Series) -> pd.Series:
    """Sign of series (-1.0, 0.0, 1.0)."""
    return np.sign(series)


@register_primitive("signed_power")
def signed_power(series: pd.Series, power: float = 2.0) -> pd.Series:
    """Signed power: sign(series) * (|series| ** power)."""
    return np.sign(series) * (series.abs() ** power)


@register_primitive("clip")
def clip_op(series: pd.Series, lower: float | None = None, upper: float | None = None) -> pd.Series:
    """Clip series between lower and upper bounds."""
    return series.clip(lower=lower, upper=upper)
