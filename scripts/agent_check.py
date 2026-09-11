#!/usr/bin/env python3
"""The research agent's only entry point. It returns pass or fail and no number.

    python scripts/agent_check.py          # does src/strategies/current.py work?

An agent that cannot execute what it writes ships syntax errors into the loop, so
it needs to be able to run something. What it must not be able to do is *score*
something, and the previous arrangement -- `run.py --trigger agent` -- got that
wrong in a way adversarial review demonstrated: `--dataset` and `--ledger` were
missing from the forbidden list, the supplied-argument check tested truthiness so
`--seed 0` read as "not supplied", and `--trigger` was a string the caller chose.
A strategy could evaluate itself against the planted-signal dataset, retry on
seeds, and write the results to a ledger that never counts.

That is not a loophole in honest N. It is a deletion of it.

**So this check produces no score.** It compiles the strategy, wires it to the
harness, walks a small committed dataset, and reports whether that worked. There
is no
Sortino, no FAR, no P&L and no blotter summary in the output, because a check
that yields no number cannot be used for selection -- there is nothing to
optimise against, nothing to retry for, and nothing to forge. Scoring happens on
the loop driver's path, which the agent does not invoke.

A deliberately small dataset, not the full validation split: enough to prove the
callbacks fire and the orders are well formed, not enough to be a result even if
someone squinted at the exit code. The size is in the committed profile, not here.

Every invocation still appends a ledger row, with `purpose: check`. It is not a
trial and does not count toward N, and it is recorded for the same reason
everything else is -- a run that happened and left no trace is the thing the
ledger exists to prevent.
"""

from __future__ import annotations

import argparse
import sys
import time
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import run as runner  # noqa: E402

from src.core.engine import Engine  # noqa: E402
from src.core.types import Split  # noqa: E402

CHECK_PROFILE = "agent"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ledger", type=Path, default=None,
                    help="for tests; the committed ledger otherwise")
    args = ap.parse_args(argv)

    cfg = runner.load_config()
    profile = cfg["profiles"][CHECK_PROFILE]
    ledger_path = args.ledger or (REPO_ROOT / cfg.get("ledger", "runs/ledger.jsonl"))

    started = time.monotonic()
    outcome, error, sessions = "passed", None, 0
    record: dict = {}
    try:
        dataset = runner.build_dataset(cfg, profile["dataset"], profile["seed"])
        strategy = runner.load_strategy()
        engine = Engine(
            dataset, strategy,
            split=Split(profile["split"]),
            fill_model=profile["fill_model"],
            warmup_sessions=profile.get("warmup_sessions", 0),
            seed=profile["seed"],
            wall_clock_budget_s=profile.get("wall_clock_budget_s"),
        )
        engine.run()
        record = engine.record.as_dict()
        sessions = len(engine.record.end_of_session_positions)
    except SystemExit as exc:
        outcome, error = "failed", f"configuration: {exc}"
    except Exception as exc:
        outcome = "failed"
        error = f"{type(exc).__name__}: {exc}"
        traceback.print_exc(limit=6)

    # Whatever the engine recorded, the score never reaches the row and never
    # reaches the caller. The ledger keeps the run; the agent gets a verdict.
    record.update(
        outcome=f"check_{outcome}", error=error, score=None, diagnostics=None,
        wall_clock_s=round(time.monotonic() - started, 6),
    )
    runner.append_ledger(
        ledger_path,
        runner._row(record, "agent", CHECK_PROFILE, ledger_path, purpose="check"),
    )

    print(f"strategy   src/strategies/current.py")
    print(f"check      {outcome.upper()}   sessions walked {sessions}")
    if error:
        print(f"reason     {error}")
    print("no score is reported by this path, by design")
    print(f"ledger     {ledger_path}")
    return 0 if outcome == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
