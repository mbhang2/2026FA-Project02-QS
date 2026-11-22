"""`config/backtest.yaml`: what a backtest uses, compiled to an ordinary profile.

Each entry states its market, split, whether it uses quotes, and its execution terms. It
becomes a profile with the same keys as `config/run.yaml`, so the engine, the gates and
the ledger have one way of reading a run.

**`use_quotes: true` never falls back to trades.** A dataset with no book is refused when
the file is read, and `require_quote_data` refuses before any run starts.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ._roots import repo_root
from .costs import apply_costs

BACKTEST_PATH = repo_root() / "config" / "backtest.yaml"
BOOK_FILL_MODEL = "book-queue-pessimistic"
ALLOWED_KEYS = {
    "dataset", "split", "use_quotes", "fill_model", "costs", "seed", "warmup_sessions",
    "wall_clock_budget_s", "latency_ns", "allow_overnight", "scoring_units",
    "idle_charge_fraction", "funding", "record_fill_spread", "action_cap", "history_window",
}
REQUIRED_KEYS = {"dataset", "split", "use_quotes", "costs"}


class BacktestConfigError(ValueError):
    """`config/backtest.yaml` asks for something that cannot run as written."""


def compile_backtests(cfg: dict, path: Path | None = None) -> dict:
    """Add each backtest to `cfg["profiles"]`, in place. Returns `cfg`."""
    path = path or BACKTEST_PATH
    if not path.exists():
        return cfg
    doc = yaml.safe_load(path.read_text()) or {}
    defaults = doc.get("defaults") or {}
    profiles, datasets = cfg["profiles"], cfg.get("datasets") or {}
    for name, entry in (doc.get("backtests") or {}).items():
        merged = {**defaults, **(entry or {})}
        unknown = set(merged) - ALLOWED_KEYS
        missing = REQUIRED_KEYS - set(merged)
        if unknown or missing:
            raise BacktestConfigError(f"backtest {name!r}: unknown keys {sorted(unknown)}, "
                                      f"missing keys {sorted(missing)}")
        if name in profiles:
            raise BacktestConfigError(f"backtest {name!r} has the name of an existing profile")
        if merged["dataset"] not in datasets:
            raise BacktestConfigError(f"backtest {name!r}: no dataset {merged['dataset']!r}")
        if not isinstance(merged["use_quotes"], bool):
            raise BacktestConfigError(f"backtest {name!r}: use_quotes must be true or false")
        has_book = bool(datasets[merged["dataset"]].get("book"))
        if merged["use_quotes"]:
            if not has_book:
                raise BacktestConfigError(f"backtest {name!r}: use_quotes is true and dataset "
                                          f"{merged['dataset']!r} has no quotes")
            merged.setdefault("fill_model", BOOK_FILL_MODEL)
            if merged["fill_model"] != BOOK_FILL_MODEL:
                raise BacktestConfigError(f"backtest {name!r}: quotes need {BOOK_FILL_MODEL}")
        else:
            merged.setdefault("fill_model", "unfavorable")
            if merged["fill_model"] == BOOK_FILL_MODEL:
                raise BacktestConfigError(f"backtest {name!r}: {BOOK_FILL_MODEL} needs "
                                          "use_quotes: true")
        profiles[name] = merged
    return cfg


def prepare_config(cfg: dict, *, backtest_path: Path | None = None,
                   costs_path: Path | None = None) -> dict:
    """The run configuration as every entry point should read it."""
    return apply_costs(compile_backtests(cfg, backtest_path), costs_path)


def quote_data_problem(profile: dict, dataset, split, warmup: int) -> str | None:
    """Why a `use_quotes` profile cannot run, or None. Checked before any run starts."""
    if not profile.get("use_quotes"):
        return None
    if not dataset.capabilities().has_book or not hasattr(dataset, "book_for"):
        return f"use_quotes is true and {dataset.name} has no quote data"
    if profile.get("fill_model") != BOOK_FILL_MODEL:
        return f"use_quotes is true and the fill model is {profile.get('fill_model')!r}"
    available = {d.toordinal() for d in dataset.sessions()}
    wanted = set(dataset.scored_sessions(split, warmup)) | set(dataset.warmup_sessions(split, warmup))
    missing = sorted(wanted - available)
    if not wanted or missing:
        return f"{len(missing)} session(s) have no quote data" if missing else "no sessions"
    return None
