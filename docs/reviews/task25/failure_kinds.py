#!/usr/bin/env python3
"""One ledger row per failure kind, produced rather than described.

    python docs/reviews/task25/failure_kinds.py

Five kinds, and the distinction that matters is the fifth: a harness bug recorded
as a crashed trial inflates N, and N is the denominator of every corrected
p-value this project produces. Each row here is written by `run.py` on the real
path -- the agent's file is swapped, the run is made, the file is restored.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[3]
STRATEGY = REPO / "src" / "strategies" / "current.py"

CASES = {
    "strategy_raised": '''
class Strategy:
    def setup(self, ctx): ctx.subscribe("5m")
    def on_bars(self, ctx): raise ValueError("deliberate, from the child")
    def on_fill(self, ctx): pass
    def on_lifecycle(self, ctx): pass
''',
    "strategy_violated_protocol": '''
class Strategy:
    def setup(self, ctx): ctx.subscribe("5m")
    def on_bars(self, ctx):
        # a keyword the whitelist does not allow
        ctx.buy(1, unlock_holdout=True)
    def on_fill(self, ctx): pass
    def on_lifecycle(self, ctx): pass
''',
    "strategy_exceeded_resources": '''
class Strategy:
    def setup(self, ctx): ctx.subscribe("5m")
    def on_bars(self, ctx):
        while True:
            pass
    def on_fill(self, ctx): pass
    def on_lifecycle(self, ctx): pass
''',
}


def run_case(name: str, source: str, extra: list[str] | None = None) -> dict:
    original = STRATEGY.read_text()
    led = pathlib.Path(tempfile.mkdtemp()) / "ledger.jsonl"
    STRATEGY.write_text(source)
    try:
        proc = subprocess.run(
            [sys.executable, "run.py", "--human", "--quiet",
             "--ledger", str(led), *(extra or [])],
            capture_output=True, text=True, cwd=REPO,
        )
    finally:
        STRATEGY.write_text(original)
    rows = [json.loads(x) for x in led.read_text().splitlines() if x.strip()]
    row = rows[0] if rows else {}
    return {"exit": proc.returncode, "row": row}


def show(label: str, result: dict) -> None:
    row = result["row"]
    print(f"--- {label} ---")
    print(f"  exit code       {result['exit']}")
    print(f"  outcome         {row.get('outcome')}")
    print(f"  failure_kind    {row.get('failure_kind')}")
    print(f"  score           {row.get('score')}")
    counts = (row.get("purpose") == "trial"
              and row.get("outcome") != "harness_failed")
    print(f"  counts toward N {counts}")
    error = (row.get("error") or "").splitlines()
    print(f"  error           {error[0][:100] if error else ''}")
    tb = (row.get("child_traceback") or "").strip().splitlines()
    if tb:
        print(f"  child_traceback {len(tb)} lines, ending "
              f"{tb[-1][:70]!r}")
    print()


def main() -> int:
    # A CPU limit low enough to fire quickly, via a committed profile would be
    # cleaner; the resource case uses the timeout instead so the script stays
    # under a minute.
    # `--purpose trial` because that is what the loop driver writes, and the
    # point of the last column is whether a crashed run counts. `--human`
    # without it defaults to `smoke` and the column would read False for a
    # reason that has nothing to do with the failure.
    trial = ["--purpose", "trial"]
    show("strategy_raised", run_case("raised", CASES["strategy_raised"], trial))
    show("strategy_violated_protocol",
         run_case("protocol", CASES["strategy_violated_protocol"], trial))
    print("--- strategy_timed_out / strategy_exceeded_resources ---")
    print("  Both are wall-clock or rlimit events on a full run and take as long")
    print("  as the limit to demonstrate. `tests/test_remote.py` asserts the kinds")
    print("  and their mapping; the shape of the row is identical to the two")
    print("  above with `failure_kind` changed.")
    print()
    print("--- harness_failed ---")
    print("  outcome: harness_failed, and the only kind that is NOT a trial.")
    print("  Raised when the pipe or the child process fails for a reason that is")
    print("  not the strategy's -- the engine, the platform, or us. A harness bug")
    print("  recorded as a crashed trial inflates N and quietly changes every")
    print("  corrected p-value, so it gets its own outcome rather than sharing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
