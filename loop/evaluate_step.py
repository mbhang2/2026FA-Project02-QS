"""The step that was missing: hand the built file to the harness and get a row.

**What this closes.** `paths.STRATEGY_FILE = "current.py"` and a comment saying
"the final file must land in `src/strategies/current.py`" were the whole of this
loop's connection to the harness. It planned, it built, it reviewed -- and then
it stopped. Nothing scored a candidate, nothing wrote a ledger row, and the
claim that this loop drives the harness was an intention in a comment.

**Why it is a subprocess and not an import.** This module imports nothing from
`src.core`. It writes a file and runs `run.py`, which is the entire interface the
harness offers a driver, and doing it without an import is the demonstration
that the interface is real rather than a shared-module convention. A second
independent driver over one harness is worth more as evidence than as
convenience.

**Why the row it produces is a real one.** `run.py` mints the `RunTicket`,
fingerprints the code and the artifacts, starts the strategy in a sandboxed
child, reconstructs the blotter from the fill log and reconciles it, and appends
exactly one ledger row on every path out -- including the ones that raise. That
is what `scripts/ledger_check.py` verifies and what makes N honest. **This is
the difference between emitting rows and emitting verifiable rows**, and it is
why this step exists rather than a shim that writes the same JSON shape.

**The file is restored afterwards, always.** `src/strategies/current.py` is the
agent-writable file and it is also an `ARTIFACT_GLOBS` entry, so leaving a
candidate in it would change the artifact fingerprint of every subsequent run.
The `finally` is not tidiness.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from loop import paths

HARNESS_STRATEGY = paths.REPO_ROOT / "src" / "strategies" / "current.py"
LEDGER = paths.REPO_ROOT / "runs" / "ledger.jsonl"


class NothingToEvaluate(FileNotFoundError):
    """The worker produced no file, so there is nothing to score.

    Deliberately not "score an empty file": a candidate that does not exist is
    not a trial, and a row for it would be a row about the loop rather than
    about a proposal.
    """


def _rows(ledger: Path) -> list[dict]:
    if not ledger.exists():
        return []
    return [json.loads(x) for x in ledger.read_text().splitlines() if x.strip()]


def run_evaluate_step(candidate_id: str, *, profile: str = "campaign",
                      ledger: Path | None = None) -> dict:
    """Score the candidate's built file. Returns the ledger row `run.py` wrote.

    One call, one row, whatever happens -- a candidate that crashes gets
    `outcome: crashed` and still counts, which is the accounting the whole
    project rests on.
    """
    ledger = ledger or LEDGER
    built = paths.candidate_file(candidate_id, paths.STRATEGY_FILE)
    if not built.exists():
        raise NothingToEvaluate(
            f"no {paths.STRATEGY_FILE} for candidate {candidate_id!r} at {built}"
        )

    before = len(_rows(ledger))
    original = HARNESS_STRATEGY.read_text()
    HARNESS_STRATEGY.write_text(built.read_text())
    try:
        subprocess.run(
            [sys.executable, "run.py", "--human", "--quiet",
             "--purpose", "trial", "--profile", profile,
             "--ledger", str(ledger)],
            capture_output=True, text=True, cwd=str(paths.REPO_ROOT),
        )
    finally:
        HARNESS_STRATEGY.write_text(original)

    rows = _rows(ledger)
    if len(rows) <= before:
        # `run.py` appends on every path out, so this cannot happen without the
        # harness itself being broken -- which is not the candidate's fault and
        # must not be recorded as its trial.
        return {"outcome": "harness_failed", "candidate_id": candidate_id,
                "error": "run.py produced no ledger row"}
    row = rows[-1]
    row["candidate_id"] = candidate_id
    return row
