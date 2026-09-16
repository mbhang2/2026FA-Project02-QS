---
name: frozen-audit
description: >-
  Run the deterministic Python auditor once for an experiment_id and read back
  the ledger row. Use before any validation prose.
disable-model-invocation: false
---

# Frozen audit

## When to use

- User runs `/audit` or asks to validate an experiment
- Before claiming PASS/FAIL on Sharpe, leak, or protocol

## Commands

From repository root:

```bash
python3 -m daksh.audit_agent list
python3 -m daksh.audit_agent run --id <experiment_id> --brief
python3 -m daksh.audit_agent ledger --id <experiment_id> --brief
```

Ticket batch (all experiments under a ticket directory):

```bash
python3 -m daksh.audit_agent ticket research/examples/tsmom-pause --brief
```

## Rules

1. Call `run` **exactly once** per validation turn.
2. Use `--brief` for slide-friendly output.
3. Do not substitute manual leak checks for the CLI.
4. Large vendor ids (e.g. `nvda-1m-realistic`): wait for completion; do not read CSV/DBN.

## Success criteria

- stdout contains gate summary and `action`
- ledger read-back returns matching `run_id` and `experiment_id`

## Failure handling

If CLI exits non-zero or prints `AUDIT_FAILED`, report FAIL and paste stderr. Do not guess metrics.
