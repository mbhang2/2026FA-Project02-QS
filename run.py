#!/usr/bin/env python3
"""The immutable entry point. `src/strategies/current.py` is a library; this runs it.

    python run.py                       # harness profile, from config/run.yaml
    python run.py --trigger agent       # what the agent may run itself
    python run.py --dataset synthetic-signal
    python run.py --profile harness --split holdout --unlock-holdout "mentor sign-off"

**Run configuration is immutable, for the same reason the scorer is.** Whoever
chooses the split, the fill model and the seed can point a run at the holdout,
select favorable fills, or retry until a lucky seed lands -- none of which
require touching the scorer, and all of which are cheaper ways to fake a result.
Everything comes from `config/run.yaml`; nothing comes from the strategy.

**The agent may run evaluations, and every one counts.** `--trigger agent` uses a
fixed profile: validation split, no argument that changes the split, the fill
model or the seed, and the holdout unreachable. An agent that cannot execute what
it writes ships syntax errors into the loop, so it gets to run -- but if it could
run fifty exploratory backtests, pick the best and present it as one candidate,
then N is fiction and the multiple-testing argument collapses silently, in the
one place nobody would look. **Every invocation appends exactly one ledger
record**, crashes included, tagged with what triggered it.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from src.core.datasets.synthetic import SyntheticDataset, SyntheticSpec  # noqa: E402
from src.core.engine import Engine  # noqa: E402
from src.core.scorer import score, score_diagnostics  # noqa: E402
from src.core.types import HoldoutLocked, Split  # noqa: E402

CONFIG_PATH = REPO_ROOT / "config" / "run.yaml"

# Arguments the agent profile refuses, by name. Each is a way to change the
# answer without changing the strategy.
AGENT_FORBIDDEN = ("split", "fill_model", "seed", "unlock_holdout", "profile")


def load_config(path: Path = CONFIG_PATH) -> dict:
    if not path.exists():
        sys.exit(f"missing run configuration at {path}")
    return yaml.safe_load(path.read_text())


def build_dataset(cfg: dict, name: str, seed: int):
    """The run's seed is the dataset's seed, so the digest in the ledger names
    the bytes the trial actually ran on rather than a default."""
    spec = cfg["datasets"].get(name)
    if spec is None:
        sys.exit(f"unknown dataset {name!r}; have {sorted(cfg['datasets'])}")
    spec = dict(spec)
    spec["seed"] = seed
    kind = spec.pop("kind")
    if kind != "synthetic":
        sys.exit(f"no adapter for dataset kind {kind!r} yet")
    return SyntheticDataset(SyntheticSpec(**spec))


def strategy_digest() -> str:
    """Content hash of the strategy source.

    git_sha alone does not identify a trial: the agent edits
    src/strategies/current.py and runs it before anything is committed, so two
    runs can share a SHA and be different code. Without this the ledger cannot
    tell two trials apart, which is the one thing it exists to do.
    """
    import hashlib

    src = REPO_ROOT / "src" / "strategies" / "current.py"
    return hashlib.sha256(src.read_bytes()).hexdigest()[:16] if src.exists() else "missing"


def load_strategy():
    """Imported by name, never handed in: the entry point decides what runs."""
    import importlib

    module = importlib.import_module("src.strategies.current")
    for attr in ("STRATEGY", "Strategy", "MomentumStrategy"):
        obj = getattr(module, attr, None)
        if obj is not None:
            return obj() if isinstance(obj, type) else obj
    sys.exit(
        "src/strategies/current.py must define a strategy class named Strategy "
        "(or expose an instance as STRATEGY)"
    )


def append_ledger(path: Path, record: dict) -> None:
    """Append-only. One line per invocation, whatever the outcome."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--trigger", choices=("harness", "agent"), default="harness",
                    help="who asked for this run; recorded in the ledger")
    ap.add_argument("--profile", help="profile from config/run.yaml (harness only)")
    ap.add_argument("--dataset", help="dataset from config/run.yaml")
    ap.add_argument("--split", choices=[s.value for s in Split],
                    help="override the profile's split (harness only)")
    ap.add_argument("--fill-model", dest="fill_model",
                    help="override the profile's fill model (harness only)")
    ap.add_argument("--seed", type=int, help="override the profile's seed (harness only)")
    ap.add_argument("--unlock-holdout", dest="unlock_holdout", metavar="REASON",
                    help="one-way, recorded in the run record (harness only)")
    ap.add_argument("--ledger", type=Path, help="override the ledger path")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    cfg = load_config()

    if args.trigger == "agent":
        supplied = [n for n in AGENT_FORBIDDEN if getattr(args, n, None)]
        if supplied:
            sys.exit(
                "an agent-triggered run takes a fixed configuration; it cannot set "
                + ", ".join(f"--{n.replace('_', '-')}" for n in supplied)
                + ". Change the strategy, not the conditions it is measured under."
            )
        profile_name = "agent"
    else:
        profile_name = args.profile or "harness"

    profile = cfg["profiles"].get(profile_name)
    if profile is None:
        sys.exit(f"unknown profile {profile_name!r}; have {sorted(cfg['profiles'])}")

    split = Split(args.split or profile["split"])
    if args.trigger == "agent" and split is not Split.VALIDATION:
        sys.exit("an agent-triggered run is validation only")

    seed = args.seed if args.seed is not None else profile["seed"]
    dataset = build_dataset(cfg, args.dataset or profile["dataset"], seed)
    if args.unlock_holdout:
        dataset.unlock_holdout(args.unlock_holdout)

    strategy = load_strategy()
    ledger_path = args.ledger or (REPO_ROOT / cfg.get("ledger", "runs/ledger.jsonl"))

    engine = Engine(
        dataset,
        strategy,
        split=split,
        fill_model=args.fill_model or profile["fill_model"],
        warmup_sessions=profile.get("warmup_sessions", 0),
        seed=seed,
    )

    started = time.monotonic()
    record: dict = {}
    try:
        engine.run()
        period = engine.session_calendar()
        result = score(engine.blotter, period=period,
                       notional_base=profile.get("notional_base", 1.0))
        engine.record.score = result.as_dict()
        engine.record.diagnostics = score_diagnostics(
            engine.blotter, period, notional_base=profile.get("notional_base", 1.0)
        ).as_dict()
        record = engine.record.as_dict()
    except HoldoutLocked as exc:
        record = _failure_record(cfg, profile_name, split, seed, args,
                                 dataset, "refused", str(exc), started)
        append_ledger(ledger_path, _row(record, args.trigger, profile_name))
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # a crash is a ledger row, not a lost trial
        rec = getattr(engine, "record", None)
        record = rec.as_dict() if rec else _failure_record(
            cfg, profile_name, split, seed, args, dataset, "crashed", repr(exc), started
        )
        append_ledger(ledger_path, _row(record, args.trigger, profile_name))
        raise
    finally:
        if record and record.get("outcome") == "completed":
            append_ledger(ledger_path, _row(record, args.trigger, profile_name))

    if not args.quiet:
        s = engine.record.score
        print(f"dataset      {engine.record.dataset}  ({engine.record.dataset_digest})")
        print(f"split        {engine.record.split}   fill model {engine.record.fill_model}")
        print(f"sessions     {s['n_sessions']}   trades {s['trade_count']}")
        print(f"{s['metric']:<13}{s['primary']:+.4f}   significance {s['significance']:+.2f}")
        print(f"sharpe       {s['sharpe']:+.4f}   net {s['net_pnl']:+.2f}")
        if s["rejected"]:
            print(f"REJECTED     {s['reject_reason']}")
        print(f"ledger       {ledger_path}")
    return 0


def _row(record: dict, trigger: str, profile: str) -> dict:
    return {
        **record,
        "trigger": trigger,
        "profile": profile,
        "strategy": "src/strategies/current.py",
        "strategy_digest": strategy_digest(),
    }


def _failure_record(cfg, profile_name, split, seed, args, dataset, outcome,
                    error, started) -> dict:
    return {
        "dataset": getattr(dataset, "name", "unknown"),
        "dataset_digest": dataset.digest() if dataset else None,
        "split": str(split),
        "seed": seed,
        "outcome": outcome,
        "error": error,
        "wall_clock_s": round(time.monotonic() - started, 6),
        "score": None,
        "diagnostics": None,
    }


if __name__ == "__main__":
    raise SystemExit(main())
