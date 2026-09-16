"""JSON **audit** ledger. One row per audit run. Same shape as a future SQLite table.

Default file: research/audit_db.json
Swap the backend later; keep record_from_row() unchanged.

**This is not the trial ledger and its rows are not trials (Task 42).** The
project counts trials in exactly one place -- `runs/ledger.jsonl`, append-only,
one row per invocation of `run.py` including the ones that crashed, filtered on
`purpose == "trial"` -- and N is the number of rows that match. Every corrected
p-value the project publishes is computed against it.

A row here records that an **audit** happened: which experiment was examined,
which gates passed, and what the auditor recommends. It carries `dsr`, `psr` and
`n_trials`, which is exactly why the distinction has to be written down rather
than assumed -- a directory of rows with an `n_trials` column is something
somebody will count, and counting these would be counting audits.

These rows also could not become trial rows by relabelling. A trial row asserts
a `RunTicket` minted by `LedgerRun`, a code fingerprint, artifact digests, a
dataset digest, an isolation level and a reconstruction that reconciles with the
accumulator -- that is what `scripts/ledger_check.py` verifies and what makes N
honest. This engine runs in process against a JSON experiment spec. Emitting
trial rows from it would grow N by rows nothing can check, which is worse than N
not growing.

Same disposition as `nalin_l1`'s trial log in Task 40, and for the same reason:
the artifact says what it is, in the artifact.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

DEFAULT_DB = Path("research/audit_db.json")

# Future SQLite:
# CREATE TABLE audit_runs (
#   run_id TEXT PRIMARY KEY,
#   ts TEXT NOT NULL,
#   experiment_id TEXT NOT NULL,
#   ticket TEXT,
#   path TEXT,
#   passed_audit INTEGER NOT NULL,
#   action TEXT NOT NULL,          -- kill | pause | modify
#   next_step TEXT,
#   verdict TEXT,
#   kind TEXT,
#   leak TEXT,
#   pit_ok INTEGER,
#   sharpe REAL,
#   psr REAL,
#   dsr REAL,
#   findings TEXT,                 -- JSON array
#   gates TEXT                     -- JSON object
# );

ACTION_FROM_RECOMMENDATION = {
    "DISCARD": "kill",
    "PAUSE": "pause",
    "ITERATE": "modify",
}


# Stated in the file, not only in the docstring: the docstring is not in the
# JSON somebody finds in six months.
NOT_A_TRIAL_LEDGER = {
    "rows_are": "audit runs, not trials",
    "counted_toward_n": False,
    "the_trial_ledger": "runs/ledger.jsonl",
    "why": (
        "An audit row records that an experiment was examined. A trial row "
        "asserts a RunTicket, a code fingerprint, artifact digests, a dataset "
        "digest, an isolation level and a reconciled reconstruction, which is "
        "what scripts/ledger_check.py verifies. N must not grow by rows nothing "
        "can check."
    ),
}


def empty_db() -> dict[str, Any]:
    return {"version": 1, "backend": "json", "ledger": NOT_A_TRIAL_LEDGER,
            "records": []}


def load_db(path: str | Path | None = None) -> dict[str, Any]:
    dest = Path(path) if path else DEFAULT_DB
    if not dest.is_file():
        return empty_db()
    try:
        payload = json.loads(dest.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return empty_db()
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
        return empty_db()
    return payload


def save_db(db: dict[str, Any], path: str | Path | None = None) -> Path:
    # Stamped on every write, so a file written before Task 42 gains the block
    # the first time it is appended to rather than staying silently unlabelled.
    db.setdefault("ledger", NOT_A_TRIAL_LEDGER)
    dest = Path(path) if path else DEFAULT_DB
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(db, indent=2, default=str) + "\n", encoding="utf-8")
    return dest


def decide_action(row: dict[str, Any]) -> tuple[str, str]:
    """kill | pause | modify for one experiment."""
    gates = row.get("gates") or {}
    findings = [str(f) for f in (row.get("findings") or [])]
    if (
        gates.get("leak") == "FAIL"
        or gates.get("pit") == "FAIL"
        or gates.get("stats") == "FAIL"
    ):
        return (
            "kill",
            "Do not continue. Illegal signal or implausible synthetic Sharpe.",
        )
    if gates.get("overall") == "FAIL" or row.get("verdict") == "FAIL":
        detail = "; ".join(findings) or "fix protocol, costs, delay, or reproduce mismatch"
        return "modify", f"Continue only after modifications: {detail}"
    if row.get("data_source") == "vendor":
        return (
            "pause",
            "Passed audit on vendor bars. Pause — this is not a capital decision.",
        )
    return (
        "pause",
        "Passed audit on this engine. Pause - do not promote; gather real data or kill.",
    )


def record_from_row(
    row: dict[str, Any],
    *,
    ticket: str | None = None,
    seed: int = 7,
) -> dict[str, Any] | None:
    exp_id = row.get("experiment_id")
    if not exp_id or exp_id == "protocol":
        return None
    action, next_step = decide_action(row)
    st = row.get("stats") or {}
    metrics = row.get("metrics") or {}
    passed = row.get("verdict") == "PASS"
    return {
        "run_id": uuid4().hex[:12],
        "ts": datetime.now(timezone.utc).isoformat(),
        "experiment_id": str(exp_id),
        "ticket": ticket or "",
        "path": str(row.get("experiment") or ""),
        "seed": seed,
        "passed_audit": passed,
        "action": action,
        "next_step": next_step,
        "verdict": row.get("verdict"),
        "kind": row.get("kind"),
        "leak": (row.get("leak") or {}).get("verdict"),
        "pit_ok": bool(row.get("pit_ok")),
        "sharpe": st.get("sharpe_annual", metrics.get("sharpe")),
        "psr": st.get("psr"),
        "dsr": st.get("dsr"),
        "findings": list(row.get("findings") or []),
        "gates": dict(row.get("gates") or {}),
    }


def records_from_packet(
    packet: dict[str, Any],
    *,
    seed: int = 7,
    ticket: str | None = None,
) -> list[dict[str, Any]]:
    ticket = ticket or (packet.get("demo") or {}).get("ticket") or ""
    if ticket:
        ticket = Path(str(ticket)).name
    out = []
    for row in packet.get("experiments") or []:
        rec = record_from_row(row, ticket=ticket, seed=seed)
        if rec:
            out.append(rec)
    if len(out) == 1 and packet.get("recommendation"):
        # single-experiment CLI: keep row-level action (more specific than ticket rec)
        pass
    return out


def append_records(
    records: list[dict[str, Any]],
    *,
    path: str | Path | None = None,
) -> Path:
    dest = Path(path) if path else DEFAULT_DB
    db = load_db(dest)
    db.setdefault("records", []).extend(records)
    return save_db(db, dest)


def latest_by_experiment(db: dict[str, Any], experiment_id: str) -> dict[str, Any] | None:
    matches = [r for r in db.get("records") or [] if r.get("experiment_id") == experiment_id]
    if not matches:
        return None
    return matches[-1]


def presentation_row(record: dict[str, Any]) -> dict[str, Any]:
    """The few fields to show on a slide."""
    return {
        "experiment_id": record.get("experiment_id"),
        "passed_audit": record.get("passed_audit"),
        "action": record.get("action"),
        "next_step": record.get("next_step"),
        "sharpe": record.get("sharpe"),
        "psr": record.get("psr"),
        "leak": record.get("leak"),
        "run_id": record.get("run_id"),
        "ticket": record.get("ticket"),
        "findings": record.get("findings") or [],
    }
