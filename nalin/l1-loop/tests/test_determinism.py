"""Determinism test (Task 5.2): identical specs produce identical results."""

from __future__ import annotations

from nalin_l1.config.experiment_spec import ExperimentSpec
from nalin_l1.orchestrator.run_experiment import run_experiment


def _spec() -> ExperimentSpec:
    return ExperimentSpec(
        asset_ids=["asset_a"],
        start="2024-01-01",
        train_end="2024-02-01",
        val_end="2024-03-01",
        end="2024-04-01",
        strategy_module_path="nalin_l1.strategy.reference_strategies.buy_and_hold",
        fee_bps=1.0,
        slippage_bps=1.0,
        output_dir="out",
    )


def test_run_experiment_deterministic():
    first = run_experiment(_spec())
    second = run_experiment(_spec())
    assert first == second
