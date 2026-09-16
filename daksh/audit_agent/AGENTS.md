# OpenCode wiring — Quant Audit Agent

This MVP lives under `daksh/audit_agent/`. The desk-wide validator in `agent-patterns/` remains for the full practicum; use **this** tree when you own the audit agent slice.

## What gets linked

| Source | OpenCode target | Role |
|--------|-----------------|------|
| `agents/quant-auditor.md` | `~/.config/opencode/agents/quant-auditor.md` | Read-only critic persona |
| `commands/audit.md` | `~/.config/opencode/commands/audit.md` | `/audit <experiment_id>` |
| `skills/frozen-audit/` | `~/.config/opencode/skills/frozen-audit/` | Run Python auditor once |
| `skills/interpret-packet/` | `~/.config/opencode/skills/interpret-packet/` | Gate reading order |

## One-time setup

From repo root:

```bash
mkdir -p ~/.config/opencode/{agents,commands,skills}
ln -sf "$(pwd)/daksh/audit_agent/agents/quant-auditor.md" ~/.config/opencode/agents/quant-auditor.md
ln -sf "$(pwd)/daksh/audit_agent/commands/audit.md" ~/.config/opencode/commands/audit.md
ln -sf "$(pwd)/daksh/audit_agent/skills/frozen-audit" ~/.config/opencode/skills/frozen-audit
ln -sf "$(pwd)/daksh/audit_agent/skills/interpret-packet" ~/.config/opencode/skills/interpret-packet
```

Restart OpenCode. Requires `LITELLM_*` env (see root `README.md`). **Model:** `claude-haiku-4-5` on this gateway.

## Usage

```text
/audit list
/audit 01-lookahead-cheat
/audit nvda-1m-realistic
```

## Independence rules

- **quant-auditor** ≠ coder/backtester model (Haiku vs Gemini Flash).
- Agent is **read-only** — ledger writes happen only inside `python3 -m daksh.audit_agent run`.
- Do not run `/desk` during `/audit`.

## Docs map

| Doc | Read when |
|-----|-----------|
| `README.md` | First visit |
| `ARCHITECTURE.md` | System design / slides |
| `GATES.md` | Implementing or explaining a gate |
| `LEDGER.md` | Storage / run_id contract |
| `examples/DEMO.md` | Professor walkthrough |
