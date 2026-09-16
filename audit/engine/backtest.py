"""Delayed-fill backtest on synthetic or vendor bars (equal-weight if many)."""

from __future__ import annotations

import importlib
from typing import Any

from audit.engine import data, leaks, metrics
from audit.engine.protocol import load_experiment

_STRATEGY_PREFIX = "qs_research.strategies"
_LOCAL_STRATEGY_PREFIX = "audit.engine.strategies"


def load_strategy(spec: str) -> Any:
    module_name, _, class_name = spec.partition(":")
    if not module_name or not class_name:
        raise ValueError(f"bad strategy spec: {spec}")
    if module_name.startswith(_STRATEGY_PREFIX):
        module_name = module_name.replace(_STRATEGY_PREFIX, _LOCAL_STRATEGY_PREFIX, 1)
    module = importlib.import_module(module_name)
    cls = getattr(module, class_name)
    return cls()


def apply_delay(signal: list[float], delay_bars: int) -> list[float]:
    if delay_bars < 0:
        raise ValueError("delay_bars must be >= 0")
    n = len(signal)
    positions = [0.0] * n
    for i in range(n):
        src = i - delay_bars
        if src >= 0:
            positions[i] = signal[src]
    return positions


def apply_costs(positions: list[float], returns: list[float], commission_bps: float) -> list[float]:
    rate = commission_bps / 10_000.0
    pnl: list[float] = []
    prev = 0.0
    for pos, ret in zip(positions, returns):
        traded = abs(pos - prev)
        pnl.append(pos * ret - traded * rate)
        prev = pos
    return pnl


def run_symbol(
    strategy: Any,
    bars,
    costs: dict[str, Any],
    *,
    periods: int = 252,
) -> dict[str, Any]:
    closes = data.closes(bars)
    rets = data.simple_returns(closes)
    signal = strategy.signal(closes)
    if len(signal) != len(closes):
        raise ValueError("strategy signal length must match bars")
    delay = int(costs["delay_bars"])
    positions = apply_delay(signal, delay)
    pnl = apply_costs(positions, rets, float(costs["commission_bps"]))
    leak = leaks.audit(
        signal,
        rets,
        delay_bars=delay,
        kind=None,
    )
    return {
        "n_bars": len(closes),
        "total_return": metrics.total_return(pnl),
        "sharpe": metrics.sharpe(pnl, periods_per_year=periods),
        "max_drawdown": metrics.max_drawdown(pnl),
        "turnover": metrics.turnover(positions),
        "signal": signal,
        "returns": rets,
        "positions": positions,
        "pnl": pnl,
        "leak": leak,
    }


def run_experiment(exp: dict[str, Any], *, seed: int = 7, include_series: bool = False) -> dict[str, Any]:
    strategy = load_strategy(exp["strategy"])
    costs = exp["costs"]
    spec = data.experiment_data_spec(exp)
    per_symbol: dict[str, Any] = {}
    periods = 252
    for symbol in exp["universe"]:
        bars = data.bars_for(exp, symbol, seed=seed)
        periods = data.periods_per_year(bars, frequency=spec["frequency"])
        per_symbol[symbol] = run_symbol(strategy, bars, costs, periods=periods)

    # Equal-weight the pnl series to the shortest length.
    lengths = [res["n_bars"] for res in per_symbol.values()]
    n = min(lengths)
    combined = [0.0] * n
    k = len(per_symbol)
    for res in per_symbol.values():
        for i in range(n):
            combined[i] += res["pnl"][i] / k

    delay = int(costs["delay_bars"])
    # Leak audit on the first symbol's raw signal (representative).
    first = next(iter(per_symbol.values()))
    leak = leaks.audit(
        first["signal"],
        first["returns"],
        delay_bars=delay,
        kind=exp.get("kind"),
    )

    wanted = set(exp["metrics"])
    summary = {
        "n_bars": n,
        "total_return": metrics.total_return(combined),
        "sharpe": metrics.sharpe(combined, periods_per_year=periods),
        "max_drawdown": metrics.max_drawdown(combined),
        "turnover": sum(res["turnover"] for res in per_symbol.values()) / k,
    }
    summary = {k: v for k, v in summary.items() if k in wanted or k == "n_bars"}

    out = {
        "experiment_id": exp["id"],
        "ticket": exp["ticket"],
        "strategy": exp["strategy"],
        "kind": exp.get("kind"),
        "data_source": spec["source"],
        "frequency": spec["frequency"],
        "costs": costs,
        "metrics": summary,
        "leak": leak,
        "symbols": {sym: {k: v[k] for k in ("n_bars", "total_return", "sharpe", "max_drawdown", "turnover")} for sym, v in per_symbol.items()},
    }
    if include_series:
        out["pnl"] = combined
    return out


def run_experiment_file(path: str, *, seed: int = 7) -> dict[str, Any]:
    return run_experiment(load_experiment(path), seed=seed)
