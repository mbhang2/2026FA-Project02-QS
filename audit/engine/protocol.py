"""Load and validate frozen experiment / protocol JSON."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REQUIRED_EXPERIMENT_KEYS = (
    "id",
    "ticket",
    "strategy",
    "universe",
    "start",
    "end",
    "costs",
    "metrics",
)
REQUIRED_COST_KEYS = ("commission_bps", "delay_bars")
ALLOWED_METRICS = ("total_return", "sharpe", "max_drawdown", "turnover", "n_bars")


class ProtocolError(ValueError):
    """Experiment JSON is missing required fields or violates the freeze."""


def load_json(path: str | Path) -> dict[str, Any]:
    raw = Path(path).read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ProtocolError(f"{path} must contain a JSON object")
    return data


def validate_experiment(exp: dict[str, Any]) -> list[str]:
    """Return a list of problems. Empty means the experiment is runnable."""
    problems: list[str] = []
    for key in REQUIRED_EXPERIMENT_KEYS:
        if key not in exp:
            problems.append(f"missing required key: {key}")
    costs = exp.get("costs")
    if not isinstance(costs, dict):
        problems.append("costs must be an object with commission_bps and delay_bars")
    else:
        for key in REQUIRED_COST_KEYS:
            if key not in costs:
                problems.append(f"costs missing {key}")
            elif not _is_number(costs[key]):
                problems.append(f"costs.{key} must be a number")
        delay = costs.get("delay_bars")
        if _is_number(delay) and delay < 0:
            problems.append("costs.delay_bars must be >= 0")
        commission = costs.get("commission_bps")
        if _is_number(commission) and commission < 0:
            problems.append("costs.commission_bps must be >= 0")
    universe = exp.get("universe")
    if not isinstance(universe, list) or not universe or not all(
        isinstance(x, str) and x for x in universe
    ):
        problems.append("universe must be a non-empty list of symbol strings")
    metrics = exp.get("metrics")
    if not isinstance(metrics, list) or not metrics:
        problems.append("metrics must be a non-empty list")
    else:
        unknown = [m for m in metrics if m not in ALLOWED_METRICS]
        if unknown:
            problems.append(f"unknown metrics: {unknown}; allowed={list(ALLOWED_METRICS)}")
    strategy = exp.get("strategy")
    if not isinstance(strategy, str) or ":" not in strategy:
        problems.append("strategy must be 'module.path:ClassName'")
    kind = exp.get("kind")
    if kind is not None and kind not in ("replicate", "realistic", "robustness", "sanity"):
        problems.append("kind must be replicate|realistic|robustness|sanity when set")
    return problems


def load_experiment(path: str | Path) -> dict[str, Any]:
    exp = load_json(path)
    problems = validate_experiment(exp)
    if problems:
        raise ProtocolError("experiment invalid:\n- " + "\n- ".join(problems))
    return exp


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
