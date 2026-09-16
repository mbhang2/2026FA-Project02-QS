# Audit gates (MVP)

Frozen in `engine/audit.py`. The LLM **interprets** this table; it does not add gates.

## Execution order

| Step | Gate | When it runs | PASS means | FAIL → action |
|------|------|--------------|------------|---------------|
| 0 | *(prerequisite)* | Load experiment JSON + PROTOCOL | Files valid | CLI error (not a research verdict) |
| 1 | **backtest** | Re-run strategy on data | Metrics computed | CLI error |
| 2 | **leak** | After backtest | No same-bar look-ahead at delay 0 | **kill** |
| 3 | **reproduce** | Compare to `reports/<id>.json` | Metrics match prior report | **modify** |
| 4 | **pit** | Mutate future closes, re-signal | Signal at t unchanged | **kill** |
| 5 | **execution** | `kind: realistic` only | delay ≥ 1, commission > 0 | **modify** |
| 6 | **freeze** | vs PROTOCOL.md | Dates/costs/files agree | **modify** |
| 7 | **stats** | PnL series | See below | **kill** only on synthetic Sharpe rule |

`overall: PASS` only if every hard gate is PASS. `stats: WEAK` does **not** fail overall.

## Gate details

### Leak

- **Test:** Pearson corr(`signal[t]`, `return[t]`) with `delay_bars == 0`.
- **FAIL if:** `|corr| ≥ 0.5` and `kind != sanity`.
- **Purpose:** Catch trading on the same bar’s return (classic cheat).
- **Note:** `corr_next_bar` is diagnostic only.

### Reproduce

- **Test:** `metrics` from fresh run vs stored report (float tolerance 1e-9).
- **SKIP if:** No prior report in `reports/`.
- **Purpose:** Backtester and auditor must agree on frozen spec.

### PIT (point-in-time)

- **Test:** Bump `closes[t+1]`; `signal[t]` must not change.
- **Sampling:** Full scan on short series; up to 64 probes on long 1-minute tapes.
- **SKIP as FAIL:** `kind: sanity` negative controls.

### Execution

- **Test:** For `kind: realistic`, require `delay_bars ≥ 1` and `commission_bps > 0`.
- **Purpose:** Distinguish paper formula (`replicate`, delay 0) from tradable claim.

### Freeze

- **Test:** `start`, `end`, `delay_bars`, `commission_bps` vs PROTOCOL; listed experiment files; trial budget (ticket audit).
- **Purpose:** Pre-registration — no metric shopping after seeing Sharpe.

### Stats

| Signal | Condition | Gate value | Overall |
|--------|-----------|------------|---------|
| PSR | < 0.95 | **WEAK** | still PASS |
| PSR | ≥ 0.95 | PASS | — |
| DSR | deflated vs `n_trials` | reported | — |
| \|Sharpe\| | > 2 on **synthetic** + realistic | **FAIL** | FAIL |
| \|Sharpe\| | any on vendor | no auto-kill | — |
| sanity kind | — | **SKIP** | — |

**Not in MVP:** OOS decay, trade count, half-sample Sharpe.

## Action mapping (after gates)

| Condition | `action` |
|-----------|----------|
| leak, pit, or stats FAIL (synthetic Sharpe) | **kill** |
| reproduce, execution, freeze FAIL | **modify** |
| overall PASS | **pause** |

PROMOTE is **not** emitted by this agent.
