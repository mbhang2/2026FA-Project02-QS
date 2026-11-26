"""Amendment 24 B5 and D6: the kernel holdout, on the Python engine, judged by the bound."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

import holdout  # noqa: E402
import run  # noqa: E402
from src.core.engine import native_trial as T  # noqa: E402


def test_the_holdout_profile_matches_train_and_is_the_reference_engine():
    profiles = run.load_config()["profiles"]
    h, t = profiles["btc-okx-mm-holdout"], profiles["btc-okx-mm-train"]
    assert (h["split"], h["engine"], h["book_events"]) == ("holdout", "reference", True)
    for key in ("inventory_cap_lots", "gate", "commission_bps", "maker_commission_bps", "latency_ns", "dataset"):
        assert h[key] == t[key], key
    assert h["wall_clock_budget_s"] >= 3 * 86_400


def test_the_reference_engine_is_for_the_holdout_only(tmp_path):
    import yaml

    from src.core.backtest_config import BacktestConfigError, compile_backtests

    entry = dict(dataset="d", split="validation", use_quotes=True, book_features=True, costs="c",
                 engine="reference", book_events=True)
    path = tmp_path / "backtest.yaml"
    path.write_text(yaml.safe_dump({"backtests": {"x": entry}}))
    with pytest.raises(BacktestConfigError, match="holdout only"):
        compile_backtests({"profiles": {}, "datasets": {"d": {"book": True}}}, path)


def test_run_py_refuses_the_kernel_holdout_without_human():
    import subprocess

    done = subprocess.run([sys.executable, str(ROOT / "run.py"), "--profile", "btc-okx-mm-holdout", "--quiet"],
                          cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert done.returncode != 0 and "holdout.py" in done.stderr


def test_the_kernel_becomes_the_audited_strategy(tmp_path):
    kernel = tmp_path / "k.py"
    kernel.write_text(T.CHECK_KERNEL)
    champ = holdout.kernel_champion({"source": kernel, "digest": "kern", "weights": None},
                                    tmp_path / "out" / "holdout-strategy.py")
    text = champ["source"].read_text()
    assert "class Strategy" in text and "def kernel(event, book, state, orders):" in text
    assert champ["kernel_digest"] == "kern" and champ["digest"] != "kern"
    namespace = {}
    exec(text, namespace)
    assert namespace["Strategy"]().state.sum() == 0.0


@pytest.mark.parametrize("returns,confirmed", [([1e-3] * 120, True), ([-1e-3] * 120, False), (None, False), ([], False)])
def test_the_holdout_verdict_is_the_bound_alone(returns, confirmed):
    import numpy as np

    if returns and returns[0] > 0:
        returns = list(np.array(returns) + np.random.default_rng(0).normal(0, 1e-4, len(returns)))
    verdict = holdout.judge_spread({"session_returns": returns})
    assert verdict["confirmed"] is confirmed and verdict["rule_ii_evaluable"] is False


def test_the_hft_kernel_holdout_profile_matches_its_train_profile():
    profiles = run.load_config()["profiles"]
    h, t = profiles["btc-okx-hft-native-holdout"], profiles["btc-okx-hft-native-train"]
    for key in ("dataset", "book_events", "commission_bps", "maker_commission_bps", "latency_ns"):
        assert h[key] == t[key], key
    assert h.get("gate") is None and t.get("gate") is None
