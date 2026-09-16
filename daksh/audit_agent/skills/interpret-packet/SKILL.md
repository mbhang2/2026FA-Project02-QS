---
name: interpret-packet
description: >-
  Read an audit evidence packet in quant desk order — reproduce and leak before
  Sharpe/PSR. Use after frozen-audit CLI output exists.
disable-model-invocation: false
---

# Interpret audit packet

## Order (mandatory)

Desk quants do **not** start from Sharpe. Read gates in this sequence:

| # | Gate | Question | FAIL → |
|---|---|---|---|
| 1 | reproduce | Fresh run matches `reports/<id>.json`? | modify |
| 2 | leak | Same-bar look-ahead with delay 0? | kill |
| 3 | pit | Signal uses future closes? | kill |
| 4 | execution | Realistic: delay ≥ 1, commission > 0? | modify |
| 5 | freeze | Matches frozen PROTOCOL? | modify |
| 6 | stats | PSR/DSR; synthetic \|Sharpe\| > 2? | WEAK or kill |

## Stats semantics

- **PSR < 0.95** → `stats_gate: WEAK` — note in prose; **not** a protocol FAIL
- **\|Sharpe\| > 2** on synthetic realistic → `stats_gate: FAIL` → **kill**
- Vendor data: implausible Sharpe kill does **not** apply

## Action mapping

| action | Meaning |
|---|---|
| kill | Stop; illegal or engine bug |
| modify | Fix and re-audit; do not rewrite PROTOCOL in place |
| pause | Legal; insufficient for PROMOTE |

## What you must quote

From CLI / ledger:

- `experiment_id`, `run_id`, `passed_audit`, `action`, `next_step`
- `gates` object (each key)
- `findings` list verbatim when non-empty
- `leak`, `pit_ok`, `sharpe`, `psr`, `dsr`

## What you must not do

- Override `passed_audit: false` because Sharpe looks good
- Call WEAK a FAIL
- Recommend PROMOTE (out of scope for this agent)
