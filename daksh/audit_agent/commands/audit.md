---
description: >-
  Post-experiment audit for one experiment_id. Frozen Python gates first, then
  structured critic verdict. Does not scout, implement, or backtest.
agent: quant-auditor
---

Audit frozen experiment results for: **$ARGUMENTS**

You are validating **already backtested** work. Do not run `/desk`, change PROTOCOL, or edit strategy code.

## Resolve argument

| `$ARGUMENTS` | Do this |
|---|---|
| empty or `list` | `python3 -m daksh.audit_agent list` → stop; ask user to re-run with an id |
| `kill` | id = `01-lookahead-cheat` |
| `pause` | id = `01-realistic` |
| path under `research/` | `python3 -m daksh.audit_agent ticket $ARGUMENTS --brief` |
| anything else | id = `$ARGUMENTS` |

## Required commands (single experiment)

```bash
python3 -m daksh.audit_agent run --id <experiment_id> --brief
python3 -m daksh.audit_agent ledger --id <experiment_id> --brief
```

Run **once**. The CLI appends one row to `research/audit_db.json`.

Do not open `NVDA_EQUITY/` raw files. Vendor 1-minute audits may take a few seconds.

## Verdict rules

- `passed_audit: false` → your headline is **FAIL**
- `action: kill` → illegal signal or implausible synthetic Sharpe
- `action: modify` → fix reproduce/protocol/costs; re-audit
- `action: pause` → legal; **not** PROMOTE
- PSR/DSR **WEAK** is not a FAIL — say so explicitly

Produce the verdict template from `agents/quant-auditor.md`.

## Demo ids

- `01-lookahead-cheat` → kill
- `01-realistic` → pause (TSMOM, synthetic)
- `nvda-1m-realistic` → pause (NVDA buy-hold, vendor)
