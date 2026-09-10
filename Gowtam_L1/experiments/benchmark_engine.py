"""Engine throughput benchmark (PLAN_NEXT A.6).

Reports bars/sec for the vectorized components at 1k–10M bars and demonstrates
near-linear scaling, plus a real end-to-end tick-data run. Records a
reproducibility block (git commit, python/dep versions, seed, dataset hash).

Run from the project root:

    .venv/bin/python experiments/benchmark_engine.py
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config.experiment_spec import ExperimentSpec
from src.execution.portfolio import build_equity_curve
from src.orchestrator.run_experiment import run_experiment
from src.strategy.protocol import Strategy


class Momentum(Strategy):
    """20-bar momentum (per-bar + vectorized) used as the scaling probe."""

    def generate_positions(self, h: pd.DataFrame) -> pd.Series:
        c = h["close"]
        sma = c.rolling(20).mean()
        ret = c / c.shift(20) - 1
        return ((c > sma) & (ret > 0)).astype(float)

    def generate_positions_vectorized(self, prices: pd.DataFrame) -> pd.Series:
        c = prices["close"]
        sma = c.rolling(20).mean()
        ret = c / c.shift(20) - 1
        signal = ((c > sma) & (ret > 0)).astype(float)
        return signal.shift(1).fillna(0.0)


def _synthetic_prices(n: int, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2000-01-01", periods=n, freq="1min", tz="UTC")
    close = 100 * np.cumprod(1 + rng.normal(0.0, 0.001, n))
    open_ = close * (1 + rng.normal(0, 0.0005, n))
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) * 1.0005,
            "low": np.minimum(open_, close) * 0.9995,
            "close": close,
            "volume": 1000.0,
        },
        index=idx,
    )


def _dataset_hash() -> str:
    try:
        from src.data.ticks import ticks_path

        p = ticks_path(2024, 1, 2)
        return hashlib.md5(open(p, "rb").read(4096)).hexdigest()
    except Exception:
        return "n/a (no tick data)"


def _reproducibility() -> dict:
    try:
        git = subprocess.check_output(["git", "rev-parse", "HEAD"]).decode().strip()
    except Exception:
        git = "unknown"
    return {
        "git_commit": git,
        "python_version": sys.version.split()[0],
        "numpy": np.__version__,
        "pandas": pd.__version__,
        "seed": 0,
        "dataset_hash": _dataset_hash(),
    }


def _report_bars_per_sec(label: str, n: int, dt: float) -> float:
    rate = n / dt if dt > 0 else float("inf")
    print(f"  {label:<38} n={n:>10,}  {dt*1000:9.1f} ms  {rate:>14,.0f} bars/s")
    return rate


def bench_equity_curve(sizes: list[int]) -> None:
    print("\n== build_equity_curve (vectorized) ==")
    prev = None
    for n in sizes:
        prices = _synthetic_prices(n)
        pos = pd.Series(1.0, index=prices.index)
        t0 = time.perf_counter()
        build_equity_curve(pos, prices, 1.0, 1.0)
        prev = _report_bars_per_sec("equity_curve", n, time.perf_counter() - t0)


def bench_strategy_scaling(sizes: list[int]) -> None:
    print("\n== strategy: per-bar (O(n^2)) vs vectorized (O(n)) ==")
    strat = Momentum()
    for n in sizes:
        prices = _synthetic_prices(n)

        t0 = time.perf_counter()
        _positions = []
        for t in range(n):
            h = prices.iloc[:t]
            _positions.append(0.0 if t == 0 else float(strat.generate_positions(h).iloc[-1]))
        dt_pb = time.perf_counter() - t0

        t0 = time.perf_counter()
        strat.generate_positions_vectorized(prices)
        dt_v = time.perf_counter() - t0

        print(
            f"  n={n:>7,}  per-bar={dt_pb*1000:9.1f} ms  vectorized={dt_v*1000:6.2f} ms"
            f"  ({dt_pb/dt_v:>8,.0f}x)"
        )


def bench_full_tick() -> None:
    print("\n== full run_experiment over real XAUUSD ticks ==")
    for freq, start, end in [
        ("1d", "2024-01-01", "2024-12-31"),
        ("1h", "2024-01-01", "2024-06-30"),
    ]:
        spec = ExperimentSpec(
            asset_ids=["xauusd"],
            start=start,
            train_end=(pd.Timestamp(start) + pd.Timedelta(days=90)).isoformat(),
            val_end=(pd.Timestamp(start) + pd.Timedelta(days=180)).isoformat(),
            end=end,
            strategy_module_path="src.strategy.reference_strategies.buy_and_hold",
            fee_bps=1.0,
            slippage_bps=1.0,
            data_source="ticks",
            data_frequency=freq,
        )
        t0 = time.perf_counter()
        result = run_experiment(spec)
        dt = time.perf_counter() - t0
        print(
            f"  freq={freq:<4} {start}..{end}  {dt:6.2f}s  "
            f"composite={result['composite_score']:.3f}"
        )


def main() -> None:
    print("Engine throughput benchmark")
    print("reproducibility:", _reproducibility())

    bench_equity_curve([10_000, 100_000, 1_000_000, 10_000_000])
    bench_strategy_scaling([1_000, 5_000, 20_000])
    bench_full_tick()


if __name__ == "__main__":
    main()
