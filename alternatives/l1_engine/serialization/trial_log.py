"""A per-candidate result artifact. **Not the trial ledger, and not N.**

`write_trial_log` writes one JSON document per candidate, under
`candidates/<id>/trial_log.json`, and `packaging` and `loop/run.py` read it back.
It is a *result* document — the shape of an eventual knowledge-base entry — and
the name is the only thing about it that suggests otherwise.

**Why that distinction is written here in capital letters (Task 40, invariant
2).** This project counts trials in exactly one place:
`runs/ledger.jsonl`, append-only, one row per invocation of `run.py` including
the ones that crashed, with `purpose == "trial"` as the filter. `N` is the
number of rows that match, and every corrected p-value the project publishes is
computed against it. A second place where trials accumulate is a second
definition of N, and the first thing anyone would do with a directory full of
`trial_log.json` files is count them.

**This engine's trials are not in that count, and this commit does not put them
there.** Wiring them in would mean emitting ledger rows from a path that has
none of what a ledger row asserts: no `RunTicket` minted by `LedgerRun`, no
`code_fingerprint`, no artifact digests, no dataset digest, no isolation record,
no reconstructed-blotter reconciliation. `scripts/ledger_check.py` would call
every such row unverifiable, and **N would grow by rows that cannot be checked**,
which is worse than N not growing at all. Porting the logging without porting
the run is how a denominator gets quietly inflated.

So the brief's other option is taken: this loop stays unintegrated, and the
artifact says so about itself. Every record now carries a `ledger` block stating
that it is not a trial-ledger row and naming what it would need to become one.

**What integration would actually take**, so the next person does not have to
re-derive it: nalin's engine runs a strategy in process against a pandas frame.
A ledger row means going through `run.py` — which means the event-driven
`Strategy` contract, the isolated child process, the fill model, and the
reconstruction. That is the port in `src/strategies/`, not a change to this
file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from alternatives.l1_engine.config.experiment_spec import ExperimentSpec

# Stated in the artifact, not only in this docstring, because the docstring is
# not in the JSON somebody finds in six months.
NOT_A_LEDGER_ROW = {
    "is_trial_ledger_row": False,
    "counted_toward_n": False,
    "the_ledger": "runs/ledger.jsonl",
    "why": (
        "This engine does not run through run.py, so it mints no RunTicket and "
        "records no code fingerprint, artifact digests, dataset digest or "
        "isolation level. A row without those is one scripts/ledger_check.py "
        "cannot verify, and N must not grow by rows that cannot be checked."
    ),
}


def write_trial_log(
    result: dict[str, Any], spec: ExperimentSpec, output_path: str
) -> str:
    """Write a structured result record and return its filesystem path.

    Includes placeholder fields for information L1 does not produce yet but that
    later stages (debate gate, KB-2, committee) will need — reserving the shape of
    the eventual results-knowledge-base entry now, so later stages extend this
    schema rather than migrate it.
    """
    record = {
        "hypothesis": None,
        "source_paper_ref": None,
        "structural_fingerprint": None,
        "verdict": None,
        "ledger": NOT_A_LEDGER_ROW,
        "spec": spec.to_dict(),
        "result": result,
    }

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(record, f, indent=2, allow_nan=False)
    return str(path)
