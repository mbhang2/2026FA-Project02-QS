"""End-to-end smoke test (Task 5.1): buy-and-hold through the full CLI path."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config.experiment_spec import ExperimentSpec
from src.orchestrator.run_experiment import run_experiment
from src.serialization.trial_log import write_trial_log


def main() -> None:
    spec = ExperimentSpec(
        data_source="ES_20250703_1m.parquet",
        asset_ids=["ES"],
        start="2025-07-03 00:00:00",
        train_end="2025-07-03 09:37:00",
        val_end="2025-07-03 14:26:00",
        end="2025-07-03 23:59:00",
        strategy_module_path="src.strategy.candidate",
        fee_bps=1.0,
        slippage_bps=1.0,
        output_dir="output",
    )

    result = run_experiment(spec)
    assert result["composite_score"] == result["composite_score"]  # no NaN

    path = write_trial_log(result, spec, "output/dummy_trial_log.json")
    print("dummy run complete; trial log:", path)
    print("validation sharpe:", result["validation_metrics"]["sharpe_ratio"])
    print("holdout sharpe:", result["holdout_metrics"]["sharpe_ratio"])
    print("decay flagged:", result["decay"]["decay_flag"])
    print("composite score:", result["composite_score"])


if __name__ == "__main__":
    main()
