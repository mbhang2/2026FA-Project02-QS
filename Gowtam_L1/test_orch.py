import sys
import pandas as pd
from src.config.experiment_spec import ExperimentSpec
from src.orchestrator.run_experiment import run_experiment

spec = ExperimentSpec(
    asset_ids=["asset_a"],
    start="2024-01-01",
    train_end="2024-02-01",
    val_end="2024-03-01",
    end="2024-04-01",
    strategy_module_path="candidates.hypothesis_md_learning_the_dynamics_of_t_20260911_153217_480131.train",
    fee_bps=1.0,
    slippage_bps=1.0,
    output_dir="output",
)
try:
    result = run_experiment(spec)
    print("Orchestrator passed")
except Exception as e:
    print(f"Orchestrator failed: {e}")
