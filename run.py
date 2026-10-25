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
import json
import platform
import sys
import time
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from src.core.data.synthetic import SyntheticDataset, SyntheticSpec  # noqa: E402
from src.core.engine import Engine, git_sha  # noqa: E402
from src.core.engine.remote import (  # noqa: E402
    HarnessFailed, RemoteFailure, RemoteFittedStrategy, RemoteStrategy,
    isolation_for,
)
from src.core.runlog import (  # noqa: E402
    LedgerRun, append_ledger, store_fill_log, store_source, store_weights,
    strategy_digest,
)
from src.core.scoring import integrity  # noqa: E402
# Bound here, at import, so a later `sys.modules[...].score = evil` does not
# reach this reference. That closes the cheapest version of the attack and not
# the general one -- these functions call into `metrics` by attribute, so the
# fingerprint below is what actually covers it.
from src.core.scoring.metrics import IDLE_CHARGE
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

# And the environment, which no digest over the repo's files can reach. Taken
# once at import rather than per row: enumerating installed distributions costs
# about 50 ms and cannot change inside a process.
ENVIRONMENT_AT_IMPORT = integrity.environment_fingerprint()
LOCKFILE_AT_IMPORT = integrity.lockfile_digest()


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
    if kind == "spx-options":
        # **Imported here, not at module scope.** The Heston pricer pulls in the
        # COS machinery and prices a whole surface on construction; a synthetic
        # or NQ run must not pay for that, and `run.py` is imported by every
        # script in the repo.
        from src.core.data.options_spx import (
            Plant, SPXHestonDataset, SPXRollingSurface, SPXSpec,
        )
        from src.core.data.synthetic import SyntheticSpec as _SSpec

        # The seed is the PATH seed here. There is no tick noise to generate:
        # the underlying is one simulated Heston path and the surface is priced
        # from it, so "the bytes this trial ran on" are chosen by which path was
        # walked. Same contract as the synthetic branch, different mechanism.
        plants = tuple(
            Plant(kind=pl["kind"], expiry_index=int(pl["expiry_index"]),
                  strike_offset=int(pl.get("strike_offset", 0)),
                  strength=float(pl["strength"]))
            for pl in spec.pop("plants", []) or ()
        )
        n_sessions = int(spec.pop("n_sessions", 40))
        # **The expiry ladder is written down, not defaulted.** Which expiries
        # a surface lists decides whether anything can ever settle, and a
        # locked-in edge is paid at settlement or never -- so it belongs in the
        # pre-registered config next to the plants rather than in a library
        # default that could move underneath a registered campaign. A grid of
        # four sessions is what puts a settlement inside train, validation AND
        # holdout on a forty-session run; see `SPXRollingSurface`.
        surface = SPXRollingSurface(
            SPXSpec(plants=plants,
                    cos_terms=int(spec.pop("cos_terms", 256)),
                    grid_points=int(spec.pop("grid_points", 601))),
            grid_sessions=int(spec.pop("expiry_every_sessions", 4)),
            n_listed=int(spec.pop("expiries_listed", 4)))
        if spec:
            sys.exit(f"unknown spx-options keys for dataset {name!r}: {sorted(spec)}")
        return SPXHestonDataset(spec=_SSpec(n_sessions=n_sessions),
                                surface=surface, path_seed=seed)
    sys.exit(f"no adapter for dataset kind {kind!r} yet")


STRATEGY_PATH = REPO_ROOT / "src" / "strategies" / "current.py"




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



def _attach_weights_safely(record: dict, engine, ledger_path: Path) -> dict:
    """`attach_weights` for the failure handlers in `_walk`.

    Those handlers exist to turn a failure into a row with an outcome. A second
    exception raised while assembling that row escapes them and the row is
    written from an empty record -- the Task 36 load-failure hole, in a new
    place. So on a failure path the weights are best-effort: if persisting them
    fails, the row says so and keeps the outcome it already had.
    """
    try:
        return attach_weights(record, engine, ledger_path)
    except Exception as exc:  # noqa: BLE001 -- recorded, never raised from a handler
        record["weights_source"] = ""
        record["weights_error"] = f"{type(exc).__name__}: {exc}"[:400]
        return record


def _proxy_for(profile: dict):
    """Which proxy class stands in for the strategy in this process.

    From the profile, because the class has to be chosen before the child exists
    to load it. That makes it a claim about code this process has not read, so
    `RemoteStrategy._check_capabilities` verifies it against what the child
    reports the class actually defines, in both directions, right after `load`.

    A run that LOADS weights also needs the fitted proxy -- it has to forward
    `load_weights`, and `Engine` then marks it already-fitted so `_fit_now`
    refuses. The first version of this function returned the ordinary proxy for
    a load run, and nq-ml-02's validation stage failed at load with the fitted
    proxy's own capability check: "defines fit() but the profile did not set
    fit_sessions". The guard was right; the routing was wrong. No look at the
    validation split was taken -- the strategy never started.
    """
    if profile.get("weights_path") or profile.get("fit_sessions", 0):
        return RemoteFittedStrategy
    return RemoteStrategy


def attach_weights(record: dict, engine, ledger_path: Path) -> dict:
    """Put a fitted strategy's weights where a later run can load them.

    Only if it actually fitted. A fitted strategy whose run crashed before the fit
    boundary has no weights, and writing an empty file would be worse than writing
    none: a later load would succeed and produce an untrained model.
    """
    if not getattr(engine, "_has_fit", False):
        return record
    record["fit_sessions"] = engine.fit_sessions
    if not getattr(engine, "_fit_completed", False):
        # `fit()` was entered and raised. There are no weights, and the child
        # that would have produced them is gone -- asking it anyway raised
        # `HarnessFailed` from inside the crash handler, past every handler,
        # and the ledger got a row with no outcome (2026-09-25). The crash is
        # the row's story; this just says the fit never finished.
        record["weights_source"] = ""
        record["fit_completed"] = False
        return record
    if getattr(engine, "_loaded_weights", False):
        # Loaded, not fitted: the weights on disk are the ones this run was
        # given, and rewriting them under this run's digest would mint a second
        # copy of a champion's model addressed by the wrong trial.
        record["weights_source"] = str(engine.weights_path or "")
        return record
    if not hasattr(engine.strategy, "save_weights"):
        # **Phase 1: the strategy fitted, but nothing can be persisted.**
        # `RemoteFittedStrategy` forwards `fit` and deliberately not
        # `save_weights` -- a campaign fits and scores inside one run and never
        # needs weights on disk. The row still records that a fit happened and
        # over how many sessions, which is what Amendment 4 counts. Persistence
        # arrives with phase 2, when an L2 champion crosses splits.
        record["weights_source"] = ""
        return record
    # **The digest is computed here, not read off the record.** `_row` adds
    # `strategy_digest` when it assembles the final row, which happens AFTER
    # every `attach_weights` call site. Reading it from the record found "" and
    # `store_weights` bailed on its `not digest` guard -- so a fitted run
    # trained, handed its weights across the boundary, and dropped them, with
    # `weights_source: ""` as the only trace. Silent, and it survived the whole
    # test suite because no unit test builds a record through `_row`. Caught by
    # the first real-NQ smoke.
    digest = record.get("strategy_digest") or strategy_digest(STRATEGY_PATH)
    path = store_weights(ledger_path, digest, engine.strategy.save_weights())
    record["weights_source"] = path
    # Recorded by digest in the row, the way `fill_log_digest` is -- and
    # deliberately NOT added to ARTIFACT_GLOBS.
    #
    # That set is compared at load and again after the run, so anything appearing
    # inside it mid-run invalidates the trial. Weights are WRITTEN during the run,
    # at the fit boundary, so globbing them would make every fitted trial
    # invalidate itself -- the same failure a stray file in config/hypotheses/
    # caused on nq-orderflow-02. Weights are an output here, not an input.
    #
    # A validation or holdout run that LOADS weights is the other case, and there
    # the digest in this row is what it must match.
    if path:
        import hashlib

        f = REPO_ROOT / path
        if f.exists():
            record["weights_digest"] = hashlib.sha256(f.read_bytes()).hexdigest()[:16]
    return record


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
    """Import the agent's file into *this* process.

    **No longer used by the scored path**, and kept only because the static check
    and its documentation refer to the names it looks for. Since Task 25 the
    strategy runs in its own process and `run.py` never imports it: this function
    was the last place on the scored path that put agent code in the harness's
    address space, which is what findings 2 and 3 of the second adversarial pass
    were both about.
    """
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

    ledger_path = args.ledger or (REPO_ROOT / cfg.get("ledger", "runs/ledger.jsonl"))

    fill_model = profile["fill_model"]

    # Every path out of this block appends exactly one row, including the ones
    # that raise. Before Task 23 that was five `append_ledger` calls in five
    # branches and a `finally` that appended for two outcomes and not the others
    # -- correct, and correct by everybody remembering. The engine cannot be
    # built outside it, because building one needs the ticket the block hands
    # out.
    with LedgerRun(
        ledger_path,
        row_builder=lambda record: _row(record, trigger, profile_name,
                                        ledger_path, purpose),
    ) as run_log:
        return _walk(args, cfg, profile, profile_name, dataset, split, seed,
                     fill_model, warmup_sessions, ledger_path, run_log)


def _walk(args, cfg, profile, profile_name, dataset, split, seed, fill_model,
          warmup_sessions, ledger_path, run_log) -> int:
    """The run itself. Split out of `main()` so the ledger block above reads as
    one thing: acquire the ticket, run, write the row.

    **Starting the strategy process is inside this try, and that is load-bearing
    (Task 36).** It used to sit in `main()`, one level out, so a strategy that
    failed to *load* -- a syntax error, no `Strategy` class, anything raised at
    module level -- raised past every handler here. `LedgerRun.__exit__` still
    wrote a row, as it promises to, but the row was built from an empty record:
    no `outcome`, no `error`, no score. `summarise()` then counted it as neither
    a trial nor a harness failure and **dropped it from N**.

    A stub that always emits valid Python never found this. An LLM finds it on
    the first malformed reply, and it shrinks the denominator by exactly the
    proposals that failed -- the cheapest way there is to cheat a
    multiple-testing correction.
    """
    started = time.monotonic()
    record: dict = {}
    engine = None
    try:
        # The strategy runs in its own process. It is not imported here and
        # never has been on this path -- `load_strategy()` used to import it,
        # which put strategy code in the harness's address space before the
        # engine existed. Findings 2 and 3 of the second adversarial pass were
        # both that sharing; this is the change that ends it.
        with _proxy_for(profile)(
            STRATEGY_PATH,
            class_name="Strategy",
            action_cap=profile.get("action_cap", 64),
            window=profile.get("history_window", 512),
            tick_window=profile.get("tick_window", 512),
            timeout_s=profile.get("wall_clock_budget_s"),
        ) as strategy:
            engine = Engine(
                dataset,
                strategy,
                ticket=run_log.ticket,
                split=split,
                fill_model=fill_model,
                warmup_sessions=warmup_sessions,
                # A fitted strategy accumulates the first `fit_sessions` of the
                # split and fits at its edge; a run that instead LOADS a champion's
                # weights passes `weights_path` and sets no fit window. Both come
                # from the committed profile, like every other evaluation
                # condition, and neither is a command-line argument -- the reason
                # `run.py`'s docstring gives at length.
                fit_sessions=profile.get("fit_sessions", 0),
                weights_path=profile.get("weights_path"),
                seed=seed,
                wall_clock_budget_s=profile.get("wall_clock_budget_s"),
                # **The profile's cost model, which this did not read until
                # Task 60.** `commission_per_contract` was declared in
                # `config/run.yaml`, copied into the campaign record, and printed
                # in the agent's run facts -- and never reached the engine, which
                # fell back to its own default of 0.01. That default is scaled for
                # the synthetic generator's price units and is TEN TIMES too cheap
                # on NQ, so `nq-mvp-01` ran at a tenth of the intended cost and
                # every artefact said otherwise.
                #
                # The default stays 0.01 in `Engine` for synthetic profiles that
                # do not set one; `_run_and_score` asserts the charged total
                # matches fills x rate, so a rate that fails to arrive is loud
                # rather than silent.
                **({"commission_per_contract": profile["commission_per_contract"]}
                   if "commission_per_contract" in profile else {}),
            )
            record = _run_and_score(cfg, profile, engine, strategy, split, seed,
                                    ledger_path)
    except _Abandoned:
        record = engine.record.as_dict()
        attach_fill_log(record, engine, ledger_path)
        attach_weights(record, engine, ledger_path)
        run_log.update(record)
        print(f"abandoned: {engine.record.error}", file=sys.stderr)
        print(f"ledger       {ledger_path}", file=sys.stderr)
        return 3
    except RemoteFailure as exc:
        # Five distinguishable kinds, and the distinction that matters is the
        # last one. A harness bug recorded as a crashed trial inflates N, and N
        # is the denominator of every corrected p-value here -- so "the strategy
        # broke" and "we broke" must never be the same row.
        rec = getattr(engine, "record", None)
        record = rec.as_dict() if rec else {}
        record.update(
            outcome=("harness_failed" if isinstance(exc, HarnessFailed)
                     else "crashed"),
            failure_kind=exc.kind, error=str(exc),
            child_traceback=getattr(exc, "child_traceback", ""),
            child_exit_status=getattr(exc, "child_exit_status", None),
            completed_callbacks=getattr(exc, "completed_callbacks", None),
            score=None, diagnostics=None,
        )
        if rec:
            attach_fill_log(record, engine, ledger_path)
            _attach_weights_safely(record, engine, ledger_path)
        run_log.update(record)
        print(f"{exc.kind}: {exc}", file=sys.stderr)
        if getattr(exc, "child_traceback", ""):
            print(exc.child_traceback, file=sys.stderr)
        return 6 if isinstance(exc, HarnessFailed) else 5
    except (integrity.ScoringMutated, integrity.ArtifactsMutated,
            ScoringMismatch) as exc:
        rec = getattr(engine, "record", None)
        record = rec.as_dict() if rec else {}
        record.update(
            outcome=_invalidation_outcome(exc),
            error=str(exc), score=None, diagnostics=None,
        )
        if rec:
            attach_fill_log(record, engine, ledger_path)
            _attach_weights_safely(record, engine, ledger_path)
        run_log.update(record)
        print(f"INVALIDATED: {exc}", file=sys.stderr)
        return 4
    except HoldoutLocked as exc:
        record = _failure_record(cfg, profile_name, split, seed, args,
                                 dataset, "refused", str(exc), started)
        run_log.update(record)
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # a crash is a ledger row, not a lost trial
        rec = getattr(engine, "record", None)
        record = rec.as_dict() if rec else _failure_record(
            cfg, profile_name, split, seed, args, dataset, "crashed", repr(exc), started
        )
        if rec:
            attach_fill_log(record, engine, ledger_path)
            _attach_weights_safely(record, engine, ledger_path)
        run_log.update(record)
        raise

    run_log.update(record)

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


def _run_and_score(cfg, profile, engine, strategy, split, seed,
                   ledger_path) -> dict:
    """Walk, reconstruct, check the fingerprints, score. Returns the record.

    Raises on every failure rather than handling any: the handlers are in
    `_walk`, and there is exactly one set of them.
    """
    engine.run()
    if engine.record.outcome == "abandoned_on_budget":
        raise _Abandoned
    period = engine.session_calendar()
    # The reconstruction, not the accumulator. `engine.blotter` is the
    # accumulator's version and is kept as a diagnostic; the number the loop
    # acts on comes from the fill log, which nothing reachable from a
    # strategy can touch.
    scored_blotter = engine.reconstructed()

    # **The rate that was configured is the rate that was charged.** Task 60:
    # `commission_per_contract` reached the record and the prompt but not the
    # engine, so a campaign ran at a tenth of its stated cost and every artefact
    # said 0.10 while the P&L said 0.01. Nothing cross-checked the two, and this
    # identity would have caught it on the first trial.
    want = profile.get("commission_per_contract", engine.commission)
    charged = sum(r.commission for r in scored_blotter.rows)
    expected = want * sum(r.size for r in scored_blotter.rows)
    if abs(charged - expected) > 1e-9 * max(1.0, abs(expected)):
        raise ScoringMismatch(
            f"the profile charges {want} per contract per side and the blotter "
            f"paid {charged:.6f} on {sum(r.size for r in scored_blotter.rows)} "
            f"contract-fills, which should be {expected:.6f}. The configured "
            "cost model did not reach the engine, so the P&L is not the P&L "
            "the record describes.")

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
    # Pre-registration Amendment 3. Read from the config, not left to the
    # default, for the reason Task 60 cost 206,710 fills' worth of commission: a
    # value declared in run.yaml that never reaches the code is a value the whole
    # record claims and nothing charges. Scoring and diagnostics must be handed
    # the same one or the diagnostics describe a different return vector.
    idle_charge = cfg.get("campaign", {}).get("idle_charge", IDLE_CHARGE)
    # Amendment 6. None when the config does not set it -> no floor. Passed to
    # BOTH score() calls below: a candidate rejected on exposure must not carry
    # an uncharged figure either.
    min_exposed = cfg.get("campaign", {}).get("min_exposed_fraction")
    result = score(scored_blotter, period=period,
                   notional_base=profile.get("notional_base", 1.0),
                   idle_charge=idle_charge,
                   min_exposed_fraction=min_exposed)
    engine.record.score = result.as_dict()
    # **Amendment 5: the same Sortino with the charge at zero, reported beside
    # the gate's number and never used by the gate.** Same blotter, same
    # calendar -- only the charge differs. A reader who sees `primary` without
    # knowing the funding cost is inside it is misled; a gate that used this
    # instead would judge the champion under a different objective than the
    # one that selected it and break train-vs-validation comparability (Task
    # 61). So both are recorded, and `primary` stays what it was.
    uncharged = score(scored_blotter, period=period,
                      notional_base=profile.get("notional_base", 1.0),
                      idle_charge=0.0,
                      min_exposed_fraction=min_exposed)
    engine.record.score["primary_uncharged"] = (
        None if uncharged.rejected else uncharged.primary)
    engine.record.diagnostics = score_diagnostics(
        scored_blotter,
        period,
        notional_base=profile.get("notional_base", 1.0),
        idle_charge=idle_charge,
        fill_model_spread=(
            _fill_model_spread(cfg, profile, seed, strategy, split, engine)
            if profile.get("record_fill_spread")
            else None
        ),
    ).as_dict()
    record = engine.record.as_dict()
    attach_fill_log(record, engine, ledger_path)
    attach_weights(record, engine, ledger_path)
    return record


def _fill_model_spread(cfg, profile, seed, strategy, split, engine) -> float:
    """Net P&L under `favorable` minus under `unfavorable`, on the same data.

    A candidate that collapses between the two modes is telling you its edge
    lives in the optimism. It costs a second full run, so it is opt-in per
    profile -- and it exists at all because `Diagnostics.fill_model_spread` was a
    field nothing ever computed.
    """
    other = "favorable" if engine.model.name == "unfavorable" else "unfavorable"
    # A second isolated process, not a second import. The twin walks the same
    # candidate under the opposite fill model and shares the ticket, because it
    # is one trial recorded in one row -- but it must not share the address
    # space, and `load_strategy()` here was the last place on this path that
    # would have put strategy code in it.
    with _proxy_for(profile)(
        STRATEGY_PATH,
        class_name="Strategy",
        action_cap=profile.get("action_cap", 64),
        window=profile.get("history_window", 512),
        tick_window=profile.get("tick_window", 512),
        timeout_s=profile.get("wall_clock_budget_s"),
    ) as twin_strategy:
        twin = Engine(
            build_dataset(cfg, profile["dataset"], seed),
            twin_strategy,
            ticket=engine.ticket,
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
        # **A row names its commit even when nothing ran.** `git_sha` used to
        # arrive only inside the engine's own record, so any run that failed
        # before the engine existed -- a strategy that raises at module level,
        # which is exactly what a BLOCKED PROPOSAL is -- produced a trial with
        # no sha. Two such rows are in the Task 36 campaigns, one per arm, and
        # `ledger_check` could say nothing about them: they count toward N and
        # cannot name the code that produced them. The engine's value wins when
        # there is one, so a successful run's row is unchanged.
        "git_sha": record.get("git_sha") or git_sha(),
        "scoring_fingerprint": integrity.fingerprint(),
        "artifact_fingerprint": integrity.artifact_fingerprint(ARTIFACTS_AT_IMPORT),
        "artifacts": ARTIFACTS_AT_IMPORT,
        "code_fingerprint": CODE_AT_IMPORT,
        # What the environment actually was, and what the repo declared it to
        # be. A difference between the two is the interesting case: a machine
        # whose pandas moved without the lockfile being regenerated.
        "environment_fingerprint": ENVIRONMENT_AT_IMPORT,
        "lockfile_digest": LOCKFILE_AT_IMPORT,
        "python_version": platform.python_version(),
        # What isolation the run actually had, not what was intended. A row that
        # says `best-effort` is a row a reader can discount; a row that does not
        # say lies by omission.
        "isolation": isolation_for().as_dict(),
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
