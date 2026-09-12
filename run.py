#!/usr/bin/env python3
"""The immutable entry point. `src/strategies/current.py` is a library; this runs it.

    python run.py                       # the harness profile, from config/run.yaml
    python run.py --profile nq-validation
    python run.py --human --purpose smoke

**This is the scored path, and the agent does not invoke it.** The agent's entry
point is `scripts/agent_check.py`, which returns pass or fail and no number.

**Every evaluation condition comes from a committed profile.** There is no
argument that selects a dataset, a split, a seed or a fill model, and that is the
fix rather than an omission. Those used to be overridable with an
agent-forbidden-list in front of them, and adversarial review showed what that is
worth: two of the flags were missing from the list, the supplied-argument check
tested truthiness so `--seed 0` read as "not supplied", and the trigger was a
string the caller chose. A strategy could evaluate itself against the
planted-signal dataset, retry on seeds, and write the result to a ledger nobody
counts -- not a loophole in honest N, a deletion of it. Adding names to a list is
a race against whoever adds the next flag; taking the values off the command line
is not.

**Every invocation appends exactly one ledger record**, crashes included, tagged
with which entry point ran and what it was for.

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
import gzip
import json
import sys
import time
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from src.core.data.synthetic import SyntheticDataset, SyntheticSpec  # noqa: E402
from src.core.engine import Engine  # noqa: E402
from src.core.scoring import integrity  # noqa: E402
# Bound here, at import, so a later `sys.modules[...].score = evil` does not
# reach this reference. That closes the cheapest version of the attack and not
# the general one -- these functions call into `metrics` by attribute, so the
# fingerprint below is what actually covers it.
from src.core.scoring.scorer import score, score_diagnostics  # noqa: E402
from src.core.types import HoldoutLocked, Split  # noqa: E402

CONFIG_PATH = REPO_ROOT / "config" / "run.yaml"

# The scoring code as committed, fingerprinted at import -- before any strategy
# module has been imported and therefore before any strategy has had a chance to
# touch it. Taking the baseline inside `main()` was the first attempt and it was
# worthless: `load_strategy()` runs before it, so a strategy that rebound a
# metric constant at import time was already in the baseline. The reproduction
# passed against that version, which is the argument for running the
# reproduction rather than reasoning about the fix.
SCORING_AT_IMPORT = integrity.fingerprint()

# And the files. `fingerprint()` watches scoring modules, which is the wrong set:
# rewriting `data/calibration/empirical-null.json` moves every accept/reject
# decision and changes no bytecode at all. Adversarial review demonstrated
# exactly that and the fingerprint stayed put.
#
# Captured here, at import, for the same reason and with one more: the *paths*
# are resolved before any strategy module exists, from a glob tuple inside the
# scoring package, so there is no list for a strategy to shorten. A set assembled
# after `load_strategy()` would be a set the strategy had a turn at.
ARTIFACTS_AT_IMPORT = integrity.artifact_digests()
CODE_AT_IMPORT = integrity.code_fingerprint()


def _invalidation_outcome(exc: Exception) -> str:
    """Which of the three integrity checks refused the trial.

    Separate outcomes rather than one `invalidated`, because the three are
    different failures with different responses: mutated scoring code is an
    attack, a fill-log disagreement is two derivations that have drifted apart,
    and a moved artifact is usually a build script that ran during a trial.
    """
    if isinstance(exc, ScoringMismatch):
        return "invalidated_fill_log_mismatch"
    if isinstance(exc, integrity.ArtifactsMutated):
        return "invalidated_artifacts_mutated"
    return "invalidated_scoring_mutated"


class ScoringMismatch(RuntimeError):
    """The fill log and the running accumulator do not agree.

    A bonus check rather than the protection -- anything that can reach the
    accumulator can reach both sides of it. It is here to catch the other
    failure: the two derivations drifting apart because one was changed and the
    other was not.
    """


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
PURPOSES = ("trial", "smoke", "replay", "check")

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
TRIGGER_PURPOSE = {"agent": "check", "loop": "trial", "harness": "trial"}
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


# The log is carried in the trial artifact so replay can *verify* the P&L rather
# than trust it, and this is the point where "where size permits" gets a number.
# A 564-session NQ trial logs a few thousand fills; a strategy sitting on the
# action cap for the whole split logs millions, and writing those to disk on
# every trial turns the ledger directory into the largest thing in the repo. Over
# the bound the row records the digest and no file: the digest still says whether
# a replay produced the same fills, which is the part that matters.
MAX_STORED_FILLS = 250_000


def store_fill_log(ledger_path: Path, digest: str, fills) -> str:
    """Keep the run's fills beside the ledger, addressed by their digest.

    Content-addressed for the same reason the sources are: a hundred replays of
    one trial cost one copy. Gzipped JSONL rather than JSON, because the file is
    append-shaped, streams, and stays readable with `zcat`.
    """
    if not fills or len(fills) > MAX_STORED_FILLS:
        return ""
    out = ledger_path.parent / "fills" / f"{digest}.jsonl.gz"
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.exists():
        with gzip.open(out, "wt") as fh:
            for fill in fills:
                fh.write(json.dumps(fill.as_row()) + "\n")
    try:
        return str(out.relative_to(REPO_ROOT))
    except ValueError:
        return str(out)


def attach_fill_log(record: dict, engine, ledger_path: Path) -> dict:
    """Put the run's fills where a replay can find them.

    Called on every path that produced an engine, crashes and invalidated trials
    included: a run that went wrong is the one whose fills you most want to look
    at, and the digest without the file only tells you that two runs differed.
    """
    if record.get("fill_log_digest"):
        record["fill_log_source"] = store_fill_log(
            ledger_path, record["fill_log_digest"], engine.fill_log
        )
    return record


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
    ap.add_argument("--profile", default="harness",
                    help="which committed profile in config/run.yaml to run")
    ap.add_argument("--human", action="store_true",
                    help="a person is running this by hand. The only mode that "
                         "may set --purpose, --ledger or --unlock-holdout.")
    ap.add_argument("--purpose", choices=PURPOSES, default=None,
                    help="--human only; defaults to 'smoke' there")
    ap.add_argument("--unlock-holdout", dest="unlock_holdout", metavar="REASON",
                    help="--human only; one-way, recorded in the run record")
    ap.add_argument("--ledger", type=Path, help="--human only")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    # There is no --dataset, --split, --seed, --fill-model or --trigger, and
    # that is the fix rather than an omission. They were overridable and the
    # agent-forbidden list did not cover all of them, so a strategy could
    # evaluate itself against the planted-signal dataset, retry on seeds and
    # write the results to a ledger nobody counts. Adding names to a
    # forbidden-list is a race against whoever adds the next flag; taking the
    # values out of the command line is not.
    #
    # Every evaluation condition now comes from the committed profile, and
    # provenance comes from which entry point ran rather than from a string the
    # caller supplies: `run.py` is the loop driver's scored path, and
    # `scripts/agent_check.py` is the agent's, which returns no number at all.
    human = bool(args.human)
    for flag in ("purpose", "unlock_holdout", "ledger"):
        if getattr(args, flag) is not None and not human:
            sys.exit(
                f"--{flag.replace('_', '-')} is for a person running the harness "
                "by hand; pass --human as well. An automated run takes its "
                "configuration from the committed profile and nothing else."
            )

    cfg = load_config()
    trigger = "human" if human else "loop"
    purpose = resolve_purpose(trigger, args.purpose)
    profile_name = args.profile

    profile = cfg["profiles"].get(profile_name)
    if profile is None:
        sys.exit(f"unknown profile {profile_name!r}; have {sorted(cfg['profiles'])}")

    split = Split(profile["split"])
    seed = profile["seed"]
    dataset = build_dataset(cfg, profile["dataset"], seed)
    if args.unlock_holdout:
        dataset.unlock_holdout(args.unlock_holdout)

    warmup_sessions = profile.get("warmup_sessions", 0)
    check_warmup_against_embargo(profile_name, warmup_sessions, dataset)

    strategy = load_strategy()
    ledger_path = args.ledger or (REPO_ROOT / cfg.get("ledger", "runs/ledger.jsonl"))

    fill_model = profile["fill_model"]
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
            attach_fill_log(record, engine, ledger_path)
            raise _Abandoned
        period = engine.session_calendar()
        # The reconstruction, not the accumulator. `engine.blotter` is the
        # accumulator's version and is kept as a diagnostic; the number the loop
        # acts on comes from the fill log, which nothing reachable from a
        # strategy can touch.
        scored_blotter = engine.reconstructed()
        reconciliation = engine.reconciliation()
        if not reconciliation["agree"]:
            raise ScoringMismatch(
                "the fill log and the accumulator disagree: "
                f"{reconciliation['accumulated_digest'][:12]} vs "
                f"{reconciliation['reconstructed_digest'][:12]}. One of the two "
                "derivations is wrong and the trial cannot say which."
            )
        integrity.check(SCORING_AT_IMPORT, integrity.fingerprint())
        integrity.check_artifacts(ARTIFACTS_AT_IMPORT, integrity.artifact_digests())
        result = score(scored_blotter, period=period,
                       notional_base=profile.get("notional_base", 1.0))
        engine.record.score = result.as_dict()
        engine.record.diagnostics = score_diagnostics(
            scored_blotter,
            period,
            notional_base=profile.get("notional_base", 1.0),
            fill_model_spread=(
                _fill_model_spread(cfg, profile, seed, strategy, split, engine)
                if profile.get("record_fill_spread")
                else None
            ),
        ).as_dict()
        record = engine.record.as_dict()
        attach_fill_log(record, engine, ledger_path)
    except _Abandoned:
        pass
    except (integrity.ScoringMutated, integrity.ArtifactsMutated,
            ScoringMismatch) as exc:
        rec = getattr(engine, "record", None)
        record = rec.as_dict() if rec else {}
        record.update(
            outcome=_invalidation_outcome(exc),
            error=str(exc), score=None, diagnostics=None,
        )
        attach_fill_log(record, engine, ledger_path)
        append_ledger(ledger_path, _row(record, trigger, profile_name,
                                        ledger_path, purpose))
        print(f"INVALIDATED: {exc}", file=sys.stderr)
        return 4
    except HoldoutLocked as exc:
        record = _failure_record(cfg, profile_name, split, seed, args,
                                 dataset, "refused", str(exc), started)
        append_ledger(ledger_path, _row(record, trigger, profile_name, ledger_path, purpose))
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # a crash is a ledger row, not a lost trial
        rec = getattr(engine, "record", None)
        record = rec.as_dict() if rec else _failure_record(
            cfg, profile_name, split, seed, args, dataset, "crashed", repr(exc), started
        )
        if rec:
            attach_fill_log(record, engine, ledger_path)
        append_ledger(ledger_path, _row(record, trigger, profile_name, ledger_path, purpose))
        raise
    finally:
        if record and record.get("outcome") in ("completed", "abandoned_on_budget"):
            append_ledger(ledger_path, _row(record, trigger, profile_name, ledger_path, purpose))

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


def _fill_model_spread(cfg, profile, seed, strategy, split, engine) -> float:
    """Net P&L under `favorable` minus under `unfavorable`, on the same data.

    A candidate that collapses between the two modes is telling you its edge
    lives in the optimism. It costs a second full run, so it is opt-in per
    profile -- and it exists at all because `Diagnostics.fill_model_spread` was a
    field nothing ever computed.
    """
    other = "favorable" if engine.model.name == "unfavorable" else "unfavorable"
    twin = Engine(
        build_dataset(cfg, profile["dataset"], seed),
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
        # Three fingerprints, three questions. `scoring_fingerprint` is the
        # bytecode of the scoring modules as they are running *now*, which is the
        # only one that can see a monkeypatch. `artifacts` is the pre-run digest
        # set, so the row states which tables and calendars produced it.
        # `code_fingerprint` hashes the engine and scoring sources on disk, and
        # is the one `scripts/ledger_check.py` can recompute at an arbitrary
        # commit to ask whether this row's code is still reachable in history.
        "scoring_fingerprint": integrity.fingerprint(),
        "artifact_fingerprint": integrity.artifact_fingerprint(ARTIFACTS_AT_IMPORT),
        "artifacts": ARTIFACTS_AT_IMPORT,
        "code_fingerprint": CODE_AT_IMPORT,
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
