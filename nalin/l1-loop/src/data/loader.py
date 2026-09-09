"""Point-in-time-safe price data loading.

This module defines the single canonical in-memory schema every downstream
component consumes, regardless of the underlying data source, behind a single
named adapter boundary (`load_prices`).

Canonical schema
----------------
A ``pandas.DataFrame`` with:

* a tz-aware UTC ``DatetimeIndex`` named ``timestamp``, sorted ascending
* columns ``asset_id`` (object), ``open``, ``high``, ``low``, ``close``,
  ``volume`` (all ``float64``)
* long format: one row per ``(timestamp, asset_id)`` pair, ``asset_id`` present
  even when only a single asset is loaded

The current implementation is a stub that reads a placeholder CSV/Parquet under
``Data/raw/``. Swapping in the real source later is a one-file change: reimplement
``load_prices`` against the real adapter while preserving this exact signature
and output schema, and no caller changes.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

# Column layout of the canonical schema. ``timestamp`` is the index.
_PRICE_COLUMNS = ["open", "high", "low", "close", "volume"]


def _data_root() -> Path:
    # ``src/data/loader.py`` -> project root is two levels up (``..``/``..``).
    return Path(__file__).resolve().parent.parent.parent


def _canonicalize(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce a raw frame into the canonical schema (index/dtTypes/columns)."""
    if "timestamp" in df.columns:
        df = df.set_index("timestamp")
    df.index = pd.to_datetime(df.index, utc=True)
    df.index.name = "timestamp"
    for col in _PRICE_COLUMNS:
        if col not in df.columns:
            df[col] = float("nan")
        df[col] = df[col].astype("float64")
    if "asset_id" not in df.columns:
        df["asset_id"] = "default"
    df = df.reset_index().sort_values(["timestamp"]).set_index("timestamp")
    df = df[["asset_id"] + _PRICE_COLUMNS]
    return df.sort_index()


def load_prices(
    source: str,
    asset_ids: list[str],
    start: str,
    end: str,
) -> pd.DataFrame:
    """Load price data into the canonical schema.

    ``source`` is the adapter boundary: it is either a filesystem path or a
    named data source. The stub implementation resolves it to a file under
    ``Data/raw/`` and reads it as CSV.

    Returns only rows for ``asset_ids`` within the inclusive ``[start, end]``
    window, sorted by timestamp.
    """
    root = _data_root()
    path = Path(source)
    if not path.is_absolute():
        path = root / "Data" / "raw" / source
    if not path.exists():
        raise FileNotFoundError(f"price data source not found: {path}")

    raw = pd.read_csv(path)
    df = _canonicalize(raw)

    mask = df["asset_id"].isin(asset_ids)
    df = df.loc[mask]

    start_ts = pd.Timestamp(start, tz="UTC")
    end_ts = pd.Timestamp(end, tz="UTC")
    df = df.loc[(df.index >= start_ts) & (df.index <= end_ts)]

    return df


def load_prices_pit(
    source: str,
    asset_ids: list[str],
    as_of: pd.Timestamp,
    start: str,
    end: str,
) -> pd.DataFrame:
    """Point-in-time-safe price loading.

    Identical to :func:`load_prices`, but raises if any loaded row has a
    ``timestamp`` greater than ``as_of``. This is the first concrete enforcement
    of the "no forward data leakage" requirement: every other component should
    call this (or a route through it), not the raw loader.
    """
    df = load_prices(source, asset_ids, start, end)
    as_of = pd.Timestamp(as_of)
    if as_of.tzinfo is None:
        as_of = as_of.tz_localize("UTC")
    else:
        as_of = as_of.tz_convert("UTC")
    future = df.index[df.index > as_of]
    if len(future) > 0:
        first_future = future[0]
        raise LookaheadError(
            f"price data contains rows past as_of={as_of}; "
            f"first offending timestamp={first_future}"
        )
    return df


class LookaheadError(Exception):
    """Raised when point-in-time loading encounters data beyond ``as_of``."""
