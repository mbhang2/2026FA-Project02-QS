# Tasks — Autonomous Hypothesis Researcher + Orchestration Loop

> Companion to `docs/PLAN.md` (the what/why) — this is the **status board** (the
> what-is-done). Task IDs match the plan. `✅` done, `🚧` in progress, `⬜` not
> started. Update this file when a task moves.

Last updated: 2026-09-25 (full build complete through Phase 7).

## Phase 1 — Scaffolding + frozen schema ✅

| ID | Task | Status |
|----|------|--------|
| T1.1 | Package skeleton `researcher/` | ✅ |
| T1.2 | `schemas.py` + validation | ✅ |
| T1.3 | `render.py` | ✅ |

## Phase 2 — Retrieval (free scholarly) ✅

| ID | Task | Status | Note |
|----|------|--------|------|
| T2.1 | `retrieval/arxiv.py` | ✅ | switched to `requests` — `urllib` hit arXiv HTTP 406 |
| T2.2 | `retrieval/semantic_scholar.py` | ✅ | 429 backoff; degrades gracefully |
| T2.3 | `retrieval/fetch.py` | ✅ | stdlib `html.parser`, no `bs4` |
| T2.4 | `retrieval/__init__.py` dedup + bind | ✅ | |

## Phase 3 — LLM client + generation passes ✅

| ID | Task | Status | Note |
|----|------|--------|------|
| T3.1 | `llm.py` gateway client | ✅ | `model_served` check + `extract_json`, verified live |
| T3.2 | `generate.py` — `decompose` | ✅ | |
| T3.3 | `generate.py` — `propose` | ✅ | |
| T3.4 | `generate.py` — `critique` | ✅ | semantic only (falsifiable/runnable/source) |
| T3.5 | `generate.py` — bounded refine | ✅ | id/sources/data frozen; prose-only rewrite |
| T3.6 | `driver.py` — `run(ask, budget)` | ✅ | live smoke emitted + wrote a file |

## Phase 4 — Firewall + pre-flight ✅

| ID | Task | Status | Note |
|----|------|--------|------|
| T4.1 | `leak_lint.py` | ✅ | caught a live `LintRejected` gate leak in smoke |
| T4.2 | `feasibility.py` | ✅ | caught a live `1h` interval drift in smoke |
| T4.3 | knowledge-window / method-only queries | ✅ | embedded in prompts + `DATA_SURFACE` |

## Phase 5 — Rail handoff + provenance ✅

| ID | Task | Status | Note |
|----|------|--------|------|
| T5.1 | write via `src.core.loop.hypothesis.lint` | ✅ | rail lint is the final authority |
| T5.2 | `research_log.jsonl` provenance | ✅ | |

## Phase 6 — Orchestration loop ✅

| ID | Task | Status | Note |
|----|------|--------|------|
| T6.1 | `orchestrator.py` `run_loop` | ✅ | whitelisted `Trajectory`; `orchestrate` pass |
| T6.2 | safety limits | ✅ | `MAX_CAMPAIGNS_PER_LOOP`, `WALL_CLOCK_S` |

## Phase 7 — Validation ✅

| ID | Task | Status | Note |
|----|------|--------|------|
| T7.1 | end-to-end smoke (arxiv + gateway) | ✅ | 1 emitted, others blocked by firewall |
| T7.2 | offline tests | ✅ | `35 passed` (pytest 9.1.1 installed) |

---

## Integration gap (explicit, not built)

- **`scripts/nq_campaign.py` hard-codes a single hypothesis path**
  (`config/hypotheses/intraday-continuation.md`). Wiring a researcher hypothesis
  into a campaign needs a `--hypothesis` argument in that script — a *main-repo*
  change outside this workstream's no-core-edit rule. `orchestrator.run_campaign`
  is the seam, defaulting to `dry_run=True`.

## Not built (explicit, per plan §5)

- The **L2 cross-campaign correction** (`max_of_m`) — owned by the main repo. The
  orchestrator runs freely, commits everything, claims nothing until it exists.
