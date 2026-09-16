# Roadmap — build on this MVP

## Done (MVP)

- [x] Three-layer model: frozen Python → hard gates → LLM critic
- [x] `quant-auditor` persona + `/audit` command + skills
- [x] CLI facade (`python3 -m audit`)
- [x] Ledger append to `research/audit_db.json`
- [x] Demo ids: cheat kill, TSMOM pause, NVDA vendor pause
- [x] Gate + ledger specs (`GATES.md`, `LEDGER.md`)

## Next (engine — `engine/`)

- [ ] SQLite ledger backend (same row shape as `LEDGER.md`)
- [ ] Optional OOS / holdout gate (must be pre-registered in PROTOCOL first)
- [ ] RTH-only filter for equity 1-minute vendor data
- [ ] Daily-resampled TSMOM on NVDA (separate experiment id)

## Next (agent)

- [ ] Committee handoff doc: when PAUSE → human PROMOTE
- [ ] Faithfulness rubric for `kind: replicate` (LLM checklist, not a gate)
- [x] `ticket` subcommand for batch audit under a ticket directory

## Explicitly out of scope

- PROMOTE from this agent
- LLM-invented statistical tests
- Reading raw multi-MB vendor files into chat context
