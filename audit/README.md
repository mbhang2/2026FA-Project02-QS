# Quant Audit Agent (MVP)

Post-experiment audit for the practicum desk: **frozen Python gates first**, then a **read-only LLM critic** that cannot override a FAIL.

**Self-contained under `audit/`** — engine, gates, backtest, strategies, ledger, CLI, and OpenCode agent wiring. Experiment JSON still lives under `research/` (data input, not engine code).

## Pipeline

```
experiment_id
    → frozen re-run + gates (Python)
    → evidence packet (JSON)
    → LLM critic (quant-auditor)
    → kill | modify | pause  (+ ledger row)
```

**Not in scope for this MVP:** PROMOTE (human/committee capital gate), OOS decay, trade-count gates.

## Quick start

From repo root:

```bash
# List auditable experiments
python3 -m audit list

# Run frozen auditor + store ledger row
python3 -m audit run --id 01-lookahead-cheat
python3 -m audit run --id nvda-1m-realistic

# Audit a full ticket directory
python3 -m audit ticket research/examples/tsmom-pause --brief

# Read back ledger
python3 -m audit ledger --id 01-lookahead-cheat
```

## OpenCode wiring (optional)

Symlink this MVP into OpenCode config:

```bash
mkdir -p ~/.config/opencode
ln -sf "$(pwd)/audit/agents/quant-auditor.md" ~/.config/opencode/agents/quant-auditor.md
ln -sf "$(pwd)/audit/commands/audit.md" ~/.config/opencode/commands/audit.md
ln -sf "$(pwd)/audit/skills/frozen-audit" ~/.config/opencode/skills/frozen-audit
ln -sf "$(pwd)/audit/skills/interpret-packet" ~/.config/opencode/skills/interpret-packet
```

Then in OpenCode:

```text
/audit 01-lookahead-cheat
/audit nvda-1m-realistic
```

## Layout

| Path | Purpose |
|---|---|
| `engine/` | Frozen gates, backtest, data, leaks, stats, strategies, ledger |
| `engine/audit.py` | Gate orchestration + `audit_experiment_id` |
| `engine/backtest.py` | Delayed-fill backtest runner |
| `engine/store.py` | `audit_db.json` ledger |
| `agents/quant-auditor.md` | LLM persona (read-only critic) |
| `commands/audit.md` | Slash command contract |
| `skills/frozen-audit/` | Run the Python auditor once |
| `skills/interpret-packet/` | Read gates in quant order |
| `facade.py`, `__main__.py` | Public API + CLI |
| `ARCHITECTURE.md`, `GATES.md`, `LEDGER.md` | Specs |
| `tests/` | Engine + facade tests |

## Demo ids

| experiment_id | Expected action | Why |
|---|---|---|
| `01-lookahead-cheat` | **kill** | Same-bar leak, implausible synthetic Sharpe |
| `01-realistic` | **pause** | Legal TSMOM on synthetic bars |
| `nvda-1m-realistic` | **pause** | Vendor NVDA buy-hold data-path check |

## Build on top

- Add gates in `engine/audit.py`, document them in `GATES.md`.
- Extend `LEDGER.md` when moving JSON → SQLite.
- Add `PROMOTE` only at committee layer, not in this agent.
