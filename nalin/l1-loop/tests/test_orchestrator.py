"""Tests for the orchestrator entrypoint and trial-log serialization (Tasks 4.1, 4.2)."""

from __future__ import annotations

import json

import pytest

from nalin_l1.config.experiment_spec import ExperimentSpec
from nalin_l1.orchestrator.run_experiment import run_experiment
from nalin_l1.serialization.trial_log import write_trial_log

BUY_HOLD = "nalin_l1.strategy.reference_strategies.buy_and_hold"


def _spec() -> ExperimentSpec:
    return ExperimentSpec(
        asset_ids=["asset_a"],
        start="2024-01-01",
        train_end="2024-02-01",
        val_end="2024-03-01",
        end="2024-04-01",
        strategy_module_path=BUY_HOLD,
        fee_bps=1.0,
        slippage_bps=1.0,
        output_dir="out",
    )


EXPECTED_METRIC_KEYS = {
    "cumulative_return",
    "sharpe_ratio",
    "annualized_volatility",
    "max_drawdown",
    "cvar",
    "stability_score",
    "per_window_sharpe",
    "turnover",
    "pnl_concentration",
}


def test_run_experiment_end_to_end():
    result = run_experiment(_spec())
    assert set(result.keys()) == {
        "validation_metrics",
        "holdout_metrics",
        "decay",
        "composite_score",
    }
    for key in ("validation_metrics", "holdout_metrics"):
        assert EXPECTED_METRIC_KEYS <= set(result[key].keys())
        for value in result[key].values():
            if isinstance(value, float):
                assert not (value != value)  # no NaN
    assert set(result["decay"].keys()) == {
        "sharpe_decay_ratio",
        "cr_decay_ratio",
        "decay_flag",
    }
    assert isinstance(result["composite_score"], float)


def test_invalid_spec_raises_before_engine():
    spec = _spec()
    spec.val_end = "2024-01-15"  # before train_end
    with pytest.raises(ValueError):
        run_experiment(spec)


def test_leakage_guard_trips():
    import pandas as pd

    from nalin_l1.execution.leakage_guard import LookaheadViolation, assert_no_lookahead
    from tests.fixtures.leaky_strategy import LeakyStrategy

    idx = pd.date_range("2024-01-01", periods=6, tz="UTC")
    prices = pd.DataFrame(
        {"open": [100] * 6, "close": [100, 101, 102, 103, 104, 105]}, index=idx
    )
    # The leaky strategy acts on the last bar it is handed. If a strategy is
    # (incorrectly) handed a full series spanning a "future" bar relative to the
    # decision point, the guard must raise before any position executes.
    with pytest.raises(LookaheadViolation):
        assert_no_lookahead(prices, idx[3])

    # Sanity: the leaky strategy itself returns a valid-shape result on full data.
    out = LeakyStrategy().generate_positions(prices)
    assert len(out) == len(prices)


def test_trial_log_round_trip(tmp_path):
    spec = _spec()
    result = run_experiment(spec)
    path = write_trial_log(result, spec, str(tmp_path / "trial_log.json"))
    with open(path) as f:
        record = json.load(f)

    assert record["hypothesis"] is None
    assert record["source_paper_ref"] is None
    assert record["structural_fingerprint"] is None
    assert record["verdict"] is None
    assert record["spec"]["asset_ids"] == ["asset_a"]
    assert record["result"]["composite_score"] == result["composite_score"]
