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
record**, crashes included, tagged with what triggered it and what it was for.

**`--purpose` classifies a run; it does not hide one.** N is the count of rows
with `purpose == "trial"`, and every other row stays in the ledger where it can
be seen and argued with. The alternative -- deleting a smoke run afterwards
because it "was not really a trial" -- is a rewrite of the record made by
whoever is being measured by it, and the first one is always defensible. An
agent-triggered run cannot set it: a trial an agent could relabel is a trial that
does not count, decided by the party with the incentive.
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

from src.core.data.synthetic import SyntheticDataset, SyntheticSpec  # noqa: E402
from src.core.engine import Engine  # noqa: E402
from src.core.scoring.scorer import score, score_diagnostics  # noqa: E402
from src.core.types import HoldoutLocked, Split  # noqa: E402

CONFIG_PATH = REPO_ROOT / "config" / "run.yaml"


class _Abandoned(Exception):
    """Internal: skip scoring a run that never finished, but still record it."""

# Arguments the agent profile refuses, by name. Each is a way to change the
# answer without changing the strategy.
AGENT_FORBIDDEN = ("split", "fill_model", "seed", "unlock_holdout", "profile",
                   "purpose")

# What a run was for. Only `trial` counts toward N; the others are recorded so
# they are classified rather than removed.
#
#   trial   an evaluation of a candidate. The unit N counts.
#   smoke   checking the harness runs at all -- a wiring change, a new adapter,
#           a demonstration. Not a candidate, not evidence about one.
#   replay  re-running a recorded trial to confirm it reproduces. Its result is
#           a statement about the first run, not a second observation.
PURPOSES = ("trial", "smoke", "replay")

# **Purpose is derived from who ran it, not accepted from the caller.**
#
# It was a flag that only agent runs were forbidden to set, which was fine while
# a human typed it and stops being fine the moment anything else does: "a trial
# an agent could relabel `smoke` is a trial that does not count" is precisely the
# loophole the field exists to close, and leaving it open for one caller leaves
# it open. So the trigger decides:
#
#   --trigger agent    always a trial. The agent runs to check its own code; that
#                      is still an evaluation against validation and it counts.
#   --trigger loop     always a trial. The loop driver's runs are the trials.
#   --trigger human    the only caller that may say, because a person running the
#                      harness by hand is the only one who can be doing something
#                      that is genuinely not a candidate.
#
# `--purpose` is therefore accepted from `--trigger human` alone, and defaults to
# `smoke` there -- a hand-run harness invocation is a smoke run unless the person
# says otherwise, which is the safe default in the direction that matters: it
# undercounts nothing, because a human claiming a trial can still say so.
TRIGGER_PURPOSE = {"agent": "trial", "loop": "trial", "harness": "trial"}
PURPOSE_MAY_BE_SET_BY = ("human",)


def resolve_purpose(trigger: str, requested: str | None) -> str:
    """What this run counts as. Derived, except for a human saying so.

    Returning a value rather than trusting one is the whole point: the ledger's
    N is the count of `trial` rows, so a caller that can choose its own label can
    choose not to be counted.
    """
    if trigger in PURPOSE_MAY_BE_SET_BY:
        return requested or "smoke"
    derived = TRIGGER_PURPOSE[trigger]
    if requested is not None and requested != derived:
        sys.exit(
            f"a {trigger}-triggered run is always a {derived!r}; it cannot "
            f"declare itself {requested!r}. N is the count of trials, so a run "
            "that could label itself out of the count would not be counted, and "
            "the multiple-testing argument is exactly that count. Use "
            "--trigger human if you are running the harness by hand."
        )
    return derived


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
    kind = spec.pop("kind")
    if kind == "synthetic":
        spec["seed"] = seed
        return SyntheticDataset(SyntheticSpec(**spec))
    if kind == "nq":
        # The seed does not reach a real adapter: there is nothing to generate.
        # It still goes in the ledger, because the profile that set it is what
        # the run was configured by.
        import os

        import boto3

        from src.core.data.nq import NQDataset, NQSpec

        missing = [v for v in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID",
                               "R2_SECRET_ACCESS_KEY", "R2_BUCKET")
                   if not os.environ.get(v)]
        if missing:
            sys.exit(
                "the NQ adapter needs read-only bucket credentials in the "
                f"environment; missing {', '.join(missing)}. See docs/setup/R2_ACCESS.md."
            )
        client = boto3.client(
            "s3",
            endpoint_url=os.environ["R2_ENDPOINT"],
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            region_name="auto",
        )
        return NQDataset(client, os.environ["R2_BUCKET"], NQSpec(**spec))
    sys.exit(f"no adapter for dataset kind {kind!r} yet")


STRATEGY_PATH = REPO_ROOT / "src" / "strategies" / "current.py"


def store_source(ledger_path: Path, digest: str,
                 strategy_path: Path | None = None) -> str:
    """Keep the strategy source beside the ledger, addressed by its digest.

    A digest identifies a trial; it does not reproduce one. To re-run trial 47
    you need its source, and at that moment the source was uncommitted and is now
    gone. Content-addressed rather than inline so a hundred runs of the same
    strategy cost one copy -- "every trial is reproducible" otherwise holds only
    for the accepted ones that survived to a commit, and the rejected ones are
    exactly what N counts.
    """
    strategy_path = strategy_path or STRATEGY_PATH
    if not strategy_path.exists():
        return ""
    out = ledger_path.parent / "sources" / f"{digest}.py"
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.exists():
        out.write_text(strategy_path.read_text())
    try:
        return str(out.relative_to(REPO_ROOT))
    except ValueError:
        # a ledger outside the repo, as in tests; keep the absolute path
        return str(out)


def strategy_digest(strategy_path: Path | None = None) -> str:
    """Content hash of the strategy source.

    git_sha alone does not identify a trial: the agent edits
    src/strategies/current.py and runs it before anything is committed, so two
    runs can share a SHA and be different code. Without this the ledger cannot
    tell two trials apart, which is the one thing it exists to do.
    """
    import hashlib

    strategy_path = strategy_path or STRATEGY_PATH
    return (
        hashlib.sha256(strategy_path.read_bytes()).hexdigest()[:16]
        if strategy_path.exists()
        else "missing"
    )


def check_warmup_against_embargo(profile_name: str, warmup: int, dataset) -> None:
    """`warmup_sessions` may not exceed the calendar's embargo.

    Warmup reaches backward from the first session of the served split. The
    embargo is the gap the calendar leaves in front of that split. So as long as
    warmup does not exceed the embargo, warmup reaches only into embargoed
    sessions and never into the previous split's scored data -- the embargo is
    enforced by construction rather than by anyone remembering the relationship.
    Exceed it and features are fitted on sessions the split was supposed to be
    separated from, which is leakage that nothing else in the harness would
    catch.

    Checked here rather than at YAML load because the embargo belongs to the
    dataset's calendar, and the earliest point both numbers exist is after the
    dataset is built. A dataset with no pre-registered calendar yet -- the NQ
    adapter, until `config/calendars/` is written -- has no embargo to check
    against, and the check stands down rather than inventing one.
    """
    try:
        calendar = dataset.calendar()
    except NotImplementedError:
        return
    if warmup > calendar.embargo_sessions:
        sys.exit(
            f"profile {profile_name!r} sets warmup_sessions={warmup}, but the "
            f"calendar embargo is {calendar.embargo_sessions} session(s). Warmup "
            "reaches backward from the start of the split, so a warmup longer "
            "than the embargo fits features on data the split was separated "
            "from. Lower the warmup or raise the embargo -- deliberately, in the "
            "pre-registered calendar."
        )


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
    ap.add_argument("--trigger", choices=("harness", "agent", "loop", "human"),
                    default="harness",
                    help="who asked for this run; recorded in the ledger, and "
                         "what its purpose is derived from")
    # Resolved after parsing, so "did not pass it" is distinguishable from
    # "passed the default".
    ap.add_argument("--purpose", choices=PURPOSES, default=None,
                    help="what the run is for; only 'trial' counts toward N. "
                         "Accepted from --trigger human alone: every other "
                         "caller's purpose is derived from the trigger.")
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
    purpose = resolve_purpose(args.trigger, args.purpose)

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

    warmup_sessions = profile.get("warmup_sessions", 0)
    check_warmup_against_embargo(profile_name, warmup_sessions, dataset)

    strategy = load_strategy()
    ledger_path = args.ledger or (REPO_ROOT / cfg.get("ledger", "runs/ledger.jsonl"))

    fill_model = args.fill_model or profile["fill_model"]
    engine = Engine(
        dataset,
        strategy,
        split=split,
        fill_model=fill_model,
        warmup_sessions=warmup_sessions,
        seed=seed,
        wall_clock_budget_s=profile.get("wall_clock_budget_s"),
    )

    started = time.monotonic()
    record: dict = {}
    try:
        engine.run()
        if engine.record.outcome == "abandoned_on_budget":
            record = engine.record.as_dict()
            raise _Abandoned
        period = engine.session_calendar()
        result = score(engine.blotter, period=period,
                       notional_base=profile.get("notional_base", 1.0))
        engine.record.score = result.as_dict()
        engine.record.diagnostics = score_diagnostics(
            engine.blotter,
            period,
            notional_base=profile.get("notional_base", 1.0),
            fill_model_spread=(
                _fill_model_spread(cfg, args, profile, seed, strategy, split, engine)
                if profile.get("record_fill_spread")
                else None
            ),
        ).as_dict()
        record = engine.record.as_dict()
    except _Abandoned:
        pass
    except HoldoutLocked as exc:
        record = _failure_record(cfg, profile_name, split, seed, args,
                                 dataset, "refused", str(exc), started)
        append_ledger(ledger_path, _row(record, args.trigger, profile_name, ledger_path, purpose))
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # a crash is a ledger row, not a lost trial
        rec = getattr(engine, "record", None)
        record = rec.as_dict() if rec else _failure_record(
            cfg, profile_name, split, seed, args, dataset, "crashed", repr(exc), started
        )
        append_ledger(ledger_path, _row(record, args.trigger, profile_name, ledger_path, purpose))
        raise
    finally:
        if record and record.get("outcome") in ("completed", "abandoned_on_budget"):
            append_ledger(ledger_path, _row(record, args.trigger, profile_name, ledger_path, purpose))

    if engine.record.outcome == "abandoned_on_budget":
        print(f"abandoned: {engine.record.error}", file=sys.stderr)
        print(f"ledger       {ledger_path}", file=sys.stderr)
        return 3

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


def _fill_model_spread(cfg, args, profile, seed, strategy, split, engine) -> float:
    """Net P&L under `favorable` minus under `unfavorable`, on the same data.

    A candidate that collapses between the two modes is telling you its edge
    lives in the optimism. It costs a second full run, so it is opt-in per
    profile -- and it exists at all because `Diagnostics.fill_model_spread` was a
    field nothing ever computed.
    """
    other = "favorable" if engine.model.name == "unfavorable" else "unfavorable"
    twin = Engine(
        build_dataset(cfg, args.dataset or profile["dataset"], seed),
        load_strategy(),
        split=split,
        fill_model=other,
        warmup_sessions=profile.get("warmup_sessions", 0),
        seed=seed,
    )
    twin.run()
    a = twin.blotter.realized_pnl - twin.blotter.commission
    b = engine.blotter.realized_pnl - engine.blotter.commission
    return (a - b) if other == "favorable" else (b - a)


def _row(record: dict, trigger: str, profile: str, ledger_path: Path,
         purpose: str = "trial", strategy_path: Path | None = None) -> dict:
    """One ledger row.

    `strategy_path` is the file that actually ran. It defaults to the agent's
    file because that is what `run.py` executes, but the harness also runs
    immutable apparatus -- the calibration reference strategy -- and a row that
    named the reference in `strategy` while digesting and storing
    `src/strategies/current.py` would replay something that never ran. The first
    real run produced exactly that row.
    """
    strategy_path = strategy_path or STRATEGY_PATH
    digest = strategy_digest(strategy_path)
    try:
        named = str(strategy_path.relative_to(REPO_ROOT))
    except ValueError:
        named = str(strategy_path)
    return {
        **record,
        "trigger": trigger,
        "purpose": purpose,
        "profile": profile,
        "strategy": named,
        "strategy_digest": digest,
        "strategy_source": store_source(ledger_path, digest, strategy_path),
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
