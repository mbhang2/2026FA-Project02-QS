"""Tests for the experiment contract: ExperimentSpec, Strategy protocol, loader."""

from __future__ import annotations

import pandas as pd
import pytest

from src.config.experiment_spec import ExperimentSpec
from src.strategy.loader import NoStrategyFoundError, load_strategy
from src.strategy.protocol import Strategy
from src.strategy.reference_strategies.buy_and_hold import BuyAndHold


def _valid_spec(**overrides) -> ExperimentSpec:
    defaults = dict(
        asset_ids=["asset_a"],
        start="2024-01-01",
        train_end="2024-02-01",
        val_end="2024-03-01",
        end="2024-04-01",
        strategy_module_path="x",
        fee_bps=1.0,
        slippage_bps=1.0,
        output_dir="out",
    )
    defaults.update(overrides)
    return ExperimentSpec(**defaults)


# --- Task 1.1: ExperimentSpec ---


def test_spec_valid_passes():
    _valid_spec().validate()


def test_spec_invalid_order_fails_distinctly():
    with pytest.raises(ValueError, match="start must be strictly before train_end"):
        _valid_spec(train_end="2023-12-31").validate()
    with pytest.raises(ValueError, match="train_end must be strictly before val_end"):
        _valid_spec(val_end="2024-01-15").validate()
    with pytest.raises(ValueError, match="val_end must be strictly before end"):
        _valid_spec(end="2024-02-15").validate()


def test_spec_empty_fields_fail():
    with pytest.raises(ValueError, match="asset_ids must be non-empty"):
        _valid_spec(asset_ids=[]).validate()
    with pytest.raises(ValueError, match="strategy_module_path must be non-empty"):
        _valid_spec(strategy_module_path="").validate()


# --- Task 1.2: Strategy protocol ---


def test_protocol_conformance():
    assert isinstance(BuyAndHold(), Strategy)


def test_protocol_rejects_missing_method():
    class NotAStrategy:
        def wrong_method(self, x):
            return 1

    assert not isinstance(NotAStrategy(), Strategy)


# --- Task 1.3: buy-and-hold ---


def test_buy_and_hold_constant_position():
    idx = pd.date_range("2024-01-01", periods=5, tz="UTC")
    df = pd.DataFrame({"close": [1, 2, 3, 4, 5]}, index=idx)
    positions = BuyAndHold().generate_positions(df)
    assert (positions == 1.0).all()
    assert list(positions.index) == list(idx)


# --- Task 1.4: loader ---


def test_load_buy_and_hold():
    strategy = load_strategy(
        "src.strategy.reference_strategies.buy_and_hold"
    )
    assert isinstance(strategy, Strategy)


def test_loader_no_conforming_class_raises():
    import importlib

    mod = importlib.import_module("src.strategy.protocol")
    with pytest.raises(NoStrategyFoundError):
        load_strategy("src.strategy.protocol")
