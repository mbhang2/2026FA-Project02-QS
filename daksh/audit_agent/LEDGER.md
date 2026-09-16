# Audit ledger

Append-only JSON stand-in for SQLite. Default path: `research/audit_db.json`.

## Row shape

Each `python3 -m daksh.audit_agent run --id …` appends one record:

```json
{
  "run_id": "dbf93cce40f8",
  "ts": "2026-09-11T13:59:37.787554+00:00",
  "experiment_id": "nvda-1m-realistic",
  "ticket": "nvda-equity",
  "path": "research/examples/nvda-equity/experiments/01-realistic.json",
  "seed": 7,
  "passed_audit": true,
  "action": "pause",
  "next_step": "Passed audit on vendor bars. Pause — this is not a capital decision.",
  "verdict": "PASS",
  "kind": "realistic",
  "leak": "PASS",
  "pit_ok": true,
  "sharpe": 1.42,
  "psr": 0.92,
  "dsr": 0.81,
  "findings": [],
  "gates": {
    "reproduce": "PASS",
    "leak": "PASS",
    "pit": "PASS",
    "execution": "PASS",
    "freeze": "PASS",
    "stats": "WEAK",
    "overall": "PASS"
  }
}
```

## Fields

| Field | Type | Meaning |
|-------|------|---------|
| `run_id` | string | Unique row id (12 hex chars) |
| `ts` | ISO UTC | When audit ran |
| `experiment_id` | string | Frozen experiment id |
| `ticket` | string | Research folder name |
| `path` | string | Path to experiment JSON |
| `seed` | int | RNG seed for synthetic data |
| `passed_audit` | bool | `verdict == PASS` |
| `action` | kill \| modify \| pause | Desk action |
| `next_step` | string | Human-readable guidance |
| `verdict` | PASS \| FAIL | Hard gates combined |
| `kind` | string | replicate \| realistic \| sanity \| … |
| `leak` | PASS \| FAIL | Leak gate summary |
| `pit_ok` | bool | Point-in-time check |
| `sharpe` | float | Annualized from auditor |
| `psr` / `dsr` | float | Track-record stats |
| `findings` | string[] | Machine findings |
| `gates` | object | Per-gate PASS/FAIL/WEAK/SKIP |

## CLI

```bash
python3 -m daksh.audit_agent ledger
python3 -m daksh.audit_agent ledger --id nvda-1m-realistic
```

## Rules

1. **One row per `run` invocation** — do not call twice in one validation turn.
2. **Immutable rows** — append only; no in-place edits.
3. **Link to critic** — OpenCode verdict must quote `run_id` from ledger read-back.

## Future (SQLite)

Keep `record_from_row()` in `engine/store.py` stable; swap JSON backend without changing agent contract.
