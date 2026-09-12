#!/usr/bin/env python3
"""The research agent's only entry point. It reads the file; it does not run it.

    python scripts/agent_check.py          # is src/strategies/current.py well formed?

An agent that cannot check what it writes ships syntax errors into the loop. What
it must not be able to do is *evaluate* what it writes, and two attempts at that
line have now failed in ways adversarial review demonstrated.

The first was `run.py --trigger agent`, with a forbidden-argument list in front
of it. Two names were missing from the list, the supplied-argument check tested
truthiness so `--seed 0` read as "not supplied", and the trigger was a string the
caller chose.

The second was this script executing the strategy against a small dataset and
reporting pass or fail and **no number** -- on the reasoning that a check
yielding no score cannot be used for selection. It can. Pass/fail is one bit, and
a strategy that raises when its own P&L crosses a threshold turns the exit code
into that bit:

    def on_fill(self, ctx):
        if ctx.realized_pnl > THRESHOLD:
            raise RuntimeError()

Eighteen probes binary-searching THRESHOLD recovered peak realized P&L to
+/-0.0002, with **zero trials counted**. It does not forge a trial; it performs
trials the system has agreed not to look at, which is the cleanest attack on
honest N there is.

Narrowing the channel does not fix it -- any output that depends on market data
can carry a score, so rate-limiting reduces bandwidth and leaves the mechanism.
So this check does not execute strategy code at all, and that rules out `import`
too: a module's top level runs on import, and a probe would simply live there.

**What runs instead** is `src.core.checks`: `compile()` for syntax, an AST pass
for structure, module-level inertness and the callback signatures, and mypy
against `src.core.types.Strategy`. Every one of those is a property of the source
text, and no amount of cleverness makes the source text a function of NQ prices.

**What the agent loses.** A runtime error is no longer free. A `TypeError` on the
first bar, an unsubscribed subscription id, an order the engine rejects -- these
now surface in a scored run, which is a trial with `outcome: crashed`,
`score: null`, and a row that counts toward N. That is intended. The project's
claim is that N includes crashed and abandoned runs, and a free crash check is a
mechanism for making crashes not count: the same hole in a friendlier shape.

Every invocation still appends a ledger row with `purpose: check`. It is not a
trial and does not count toward N, and it is recorded for the reason everything
is -- a run that happened and left no trace is what the ledger exists to prevent.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import run as runner  # noqa: E402

from src.core.checks import static_check, type_check  # noqa: E402

STRATEGY_PATH = REPO_ROOT / "src" / "strategies" / "current.py"


def check(path: Path = STRATEGY_PATH) -> tuple[bool, list[str], list[str]]:
    """`(passed, errors, notes)`. Nothing in here executes `path`."""
    report = static_check(path)
    errors = [str(f) for f in report.findings if f.code.startswith("E")]
    notes = [str(f) for f in report.findings if not f.code.startswith("E")]

    if report.strategy_class and not errors:
        # Only worth running once the file parses and names something loadable;
        # mypy on a file with a missing callback repeats what the AST already
        # said, at ten times the cost.
        result = type_check(path, report.strategy_class)
        errors.extend(result.errors)
    return not errors, errors, notes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ledger", type=Path, default=None,
                    help="for tests; the committed ledger otherwise")
    args = ap.parse_args(argv)

    cfg = runner.load_config()
    ledger_path = args.ledger or (REPO_ROOT / cfg.get("ledger", "runs/ledger.jsonl"))

    started = time.monotonic()
    passed, errors, notes = check()

    record = {
        "outcome": "check_passed" if passed else "check_failed",
        "error": errors[0] if errors else None,
        "findings": errors,
        "notes": notes,
        "score": None,
        "diagnostics": None,
        "executed_strategy_code": False,
        "wall_clock_s": round(time.monotonic() - started, 6),
    }
    runner.append_ledger(
        ledger_path,
        runner._row(record, "agent", "static", ledger_path, purpose="check"),
    )

    print("strategy   src/strategies/current.py")
    print(f"check      {'PASSED' if passed else 'FAILED'}   "
          f"static analysis only; the strategy was not executed")
    for line in errors:
        print(f"  error    {line}")
    for line in notes:
        print(f"  note     {line}")
    if passed:
        print("this path reports no score and runs no data, by design. A runtime")
        print("error costs a trial -- see docs/agent/strategy-api.md.")
    print(f"ledger     {ledger_path}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
