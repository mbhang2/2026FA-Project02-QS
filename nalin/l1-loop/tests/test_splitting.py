"""Tests for train/validation/holdout splitting (Task 2.4)."""

from __future__ import annotations

import pandas as pd

from src.config.experiment_spec import ExperimentSpec
from src.splitting.windows import split_periods


def _spec():
    return ExperimentSpec(
        asset_ids=["a"],
        start="2024-01-01",
        train_end="2024-02-01",
        val_end="2024-03-01",
        end="2024-04-01",
        strategy_module_path="x",
    )


def test_split_periods_contiguous_non_overlapping():
    train, validation, holdout = split_periods(_spec())
    assert train.start == pd.Timestamp("2024-01-01", tz="UTC")
    assert train.end == validation.start
    assert validation.end == holdout.start
    assert holdout.end == pd.Timestamp("2024-04-01", tz="UTC")
    for r in (train, validation, holdout):
        assert r.start < r.end
