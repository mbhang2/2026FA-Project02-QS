"""Tests for the price data loader (Tasks 0.2 and 0.3)."""

from __future__ import annotations

import pandas as pd
import pytest

from src.data.loader import LookaheadError, load_prices, load_prices_pit


FIXTURE = "placeholder.csv"
ASSETS = ["asset_a", "asset_b"]


def test_canonical_schema_shape():
    df = load_prices(FIXTURE, ASSETS, "2024-01-01", "2024-12-31")
    assert isinstance(df.index, pd.DatetimeIndex)
    assert df.index.tz is not None
    assert df.index.name == "timestamp"
    assert list(df.columns) == ["asset_id", "open", "high", "low", "close", "volume"]
    assert df["open"].dtype == "float64"
    assert df["high"].dtype == "float64"
    assert df["low"].dtype == "float64"
    assert df["close"].dtype == "float64"
    assert df["volume"].dtype == "float64"
    assert df.index.is_monotonic_increasing
    assert set(df["asset_id"].unique()) == set(ASSETS)


def test_asset_filtering():
    df = load_prices(FIXTURE, ["asset_a"], "2024-01-01", "2024-12-31")
    assert (df["asset_id"] == "asset_a").all()


def test_window_filtering():
    df = load_prices(FIXTURE, ASSETS, "2024-02-01", "2024-02-15")
    assert df.index.min() >= pd.Timestamp("2024-02-01", tz="UTC")
    assert df.index.max() <= pd.Timestamp("2024-02-15", tz="UTC")


def test_missing_source_raises():
    with pytest.raises(FileNotFoundError):
        load_prices("does_not_exist.csv", ASSETS, "2024-01-01", "2024-12-31")


def test_pit_mid_fixture_returns_only_earlier_rows():
    as_of = pd.Timestamp("2024-02-15", tz="UTC")
    df = load_prices_pit(FIXTURE, ASSETS, as_of, "2024-01-01", "2024-02-15")
    assert len(df) > 0
    assert df.index.max() <= as_of


def test_pit_past_end_returns_everything():
    as_of = pd.Timestamp("2024-12-31", tz="UTC")
    df = load_prices_pit(FIXTURE, ASSETS, as_of, "2024-01-01", "2024-12-31")
    full = load_prices(FIXTURE, ASSETS, "2024-01-01", "2024-12-31")
    assert len(df) == len(full)


def test_pit_raises_on_future_row():
    as_of = pd.Timestamp("2024-01-31", tz="UTC")
    with pytest.raises(LookaheadError):
        load_prices_pit(FIXTURE, ASSETS, as_of, "2024-01-01", "2024-12-31")
