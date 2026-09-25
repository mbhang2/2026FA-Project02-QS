# Autonomous Hypothesis Researcher + Orchestration Loop — Atomic Build Plan

> **Document Type**: Atomic build plan
> **Status**: DRAFT (key verified working)
> **Scope**: (1) the *researcher* — turns a one-line ask into a population of
> linted, pre-registered hypotheses; (2) the *orchestration loop* — runs those
> hypotheses through the existing campaign loop and decides what to try next.
> Both follow the **MVP orchestrator design**: plain HTTP calls to the LiteLLM
> gateway, exactly like `src/core/loop/llm.py`. No opencode invocation.

The researcher **emits hypotheses only** — never strategy code. The orchestration
loop **invokes campaigns, never re-implements them**. Both feed an **unchanged
rail**: `src/core/loop/hypothesis.py` → pre-registration →
`src/core/loop/llm.LLMAgent.propose` → campaign → the `max_of_n` gate. None of the
frozen MVP core (`src/core/`) is edited. This workstream's own code lives here in
`Gowtam_hypothesis/`, sister to `Gowtam_L1/`.

---

## 0. Runtime model: the MVP gateway (verified)

Every intelligent pass is a plain `POST {base}/chat/completions`, mirroring
`src/core/loop/llm.py` (`_one`, `preflight`). No opencode agent roles, no
tool-calling — a chat completion has **no web or tool access**, so the firewall
is about what *text* we put in a prompt and what text we accept out, not about
denying agent permissions.

**Credentials (verified live this session):**

| var | value |
|-----|-------|
| `LITELLM_BASE_URL` | `https://llm-proxy.invalid/v1` |
| `LITELLM_API_KEY` | present in `Gowtam_hypothesis/.env` (currently the variable is named `Litellm`; the driver reads `LITELLM_API_KEY` — see §7.1) |

Confirmed: `/models` returns **107 models**; `claude-haiku-4-5`
(`/chat/completions`) returns 200 with `model` echoed and a correct reply.

**The disciplines carried over from `llm.py` (they are the design, not an option):**

1. **Record `model_served`, raise on substitution.** A `claude-haiku-4-5` request
   answered by something else is a harness failure, not a research result. The
   researcher's provenance log records which model served each pass.
2. **No retries.** A failed/empty/unparseable completion is a *blocked* step,
   recorded in the research log — never silently re-dispatched.
3. **Bounded repair.** A single parsing/static failure may be repaired once or
   twice (mirroring `max_repair_attempts`); exhausting the bound is blocked.
4. **Structured parse, not persuasion.** Output is read from a fenced JSON block
   (a `<<<RESULT>>>`-style fence), never from a paragraph the model also argued for.
5. **Whitelist the history, never the gate.** The orchestration pass is shown
   campaign *trajectories* (score direction, trade count, turnover), the same
   fields `llm.py`'s `_history_block` whitelists — never p-values, accept/reject,
   N, or anything the correction computes.

---

## 1. Search backends (no paid keys)

| backend | endpoint | key? | covers |
|---------|----------|------|--------|
| **arXiv** | `export.arxiv.org/api/query` (Atom) | none | q-fin.TR / q-fin.ST / stat.ML / cs.LG preprints |
| **Semantic Scholar** | `api.semanticscholar.org/graph/v1/paper/search` | none (optional free key for higher rate) | broad paper discovery, abstracts, citations |
| **Crossref** | `api.crossref.org/works` | none | DOI + bibliographic metadata |

Plain `requests` + stdlib XML/JSON — zero new dependencies. General-web search
remains a **known gap** (§7.2), not a blocker.

---

## 2. Non-negotiables

1. **The strategy child has no network; the researcher does.** The researcher
   (driver tier) fetches papers. It must **never** put a realized eval-window
   price into a hypothesis — §4's firewall is a mechanism (extended lint),
   not a prompt sentence. A chat-completion pass has no web access, so the only
   leak vectors are the *retrieval layer* (deterministic Python we control) and
   the model's *training knowledge* (caught by `leak_lint`).
2. **"Winning hypothesis" is not the researcher's output.** The gate decides what
   wins. The researcher emits a *population* of falsifiable candidates.
3. **Output must pass the existing `hypothesis.lint()` untouched.** Rendered
   markdown goes through `src.core.loop.hypothesis.load()`, the only route into
   the rail.
4. **No unsupported assertions — every source claim carries a URL or it is dropped.**
5. **A failed generation is recorded, never silently retried.**
6. **The orchestrator never sees the gate** (whitelisted trajectories only).

---

## 3. Frozen contracts (already scaffolded)

### 3.1 `HypothesisRecord` — `researcher/schemas.py`

`SourceClaim`, `DataRequires`, `HypothesisRecord` (all `frozen=True`), with
mechanical validation: non-empty `falsifiable` (single sentence), non-empty
`sources`, `id` matching `^[A-Za-z0-9_-]+$`, every source with a URL. A
non-conformant completion is rejected by the validator, never coerced.

### 3.2 Renderer — `researcher/render.py`

`render(rec) -> str` flattens a record into `config/hypotheses/*.md` shape.
**Verified**: representative output passes the MVP's own
`src.core.loop.hypothesis.lint`.

### 3.3 Extended leak-lint — `researcher/leak_lint.py`

An additional `BANNED` list on top of the rail's, catching the researcher's
specific failure mode: date/instrument/outcome and current-price statements
("NQ declined 4% in March"). Both lints run; both must be clean.

---

## 4. Phases and atomic tasks

### Phase 1 — Scaffolding + frozen schema (done)

- **T1.1 Package skeleton** — `researcher/` (a distinct top-level name, not
  `src/researcher`, so it cannot collide with the MVP's `src` namespace on
  `sys.path`). **Done.**
- **T1.2 `schemas.py`** — **Done** (validation unit-tested by hand; see §8).
- **T1.3 `render.py`** — **Done** (verified lint-clean).

### Phase 2 — Retrieval (free scholarly)

- **T2.1 `retrieval/arxiv.py`.** stdlib `urllib` + `xml.etree` on the Atom API.
  `search(query, max_results)` + `search_by_id`. *Acceptance:* live q-fin query
  returns entries; parsing unit-tested on a captured Atom fixture.
- **T2.2 `retrieval/semantic_scholar.py`.** `requests` on the Graph API.
  *Acceptance:* live query returns papers with abstracts; rate-limit backoff tested.
- **T2.3 `retrieval/fetch.py`.** `fetch_and_extract(url) -> Extracted`.
  HTML→text via stdlib `html.parser` (no new dependency). *Acceptance:* fetches an
  arXiv abstract page and a plain whitepaper.
- **T2.4 `retrieval/__init__.py`.** Query all backends, dedup near-duplicates,
  bind claims to `SourceClaim`. *Acceptance:* a duplicated paper appears once;
  every claim has a URL.

### Phase 3 — The LLM client + generation passes (MVP design)

- **T3.1 `llm.py` — gateway client.** `call(model, messages, *, temperature,
  max_tokens) -> Completion{text, model_served, usage}` and `extract_json(text)`
  (last fenced JSON block), with the §0 disciplines: `ModelSubstituted` on
  mismatch, `LLMError` on failure, no retries inside `call`. Reads
  `LITELLM_BASE_URL`/`LITELLM_API_KEY` from env. *Acceptance:* live call returns
  200 and `model_served == model`; a mocked mismatch raises.
- **T3.2 `generate.py` — decompose.** `decompose(ask) -> list[str]` of mechanism
  families; one pass per scope, parsed as JSON `{"families": [...]}`.
  *Acceptance:* a fixed ask yields ≥3 distinct families.
- **T3.3 `generate.py` — propose.** `propose(mechanism, sources) -> HypothesisRecord`;
  the completion fills the §3.1 fields; the driver builds the record and the
  **schema validator rejects** non-conformant replies (blocked). *Acceptance:*
  conformant replies round-trip; malformed JSON → blocked.
- **T3.4 `generate.py` — critique.** `critique(rec) -> CriticVerdict`, a second,
  colder pass against the literal checklist (falsifiable / runnable / no gate
  vocab / no outcome leak / sources real). *Acceptance:* named failures
  (`not_falsifiable`, `leaks_gate`, `leaks_outcome`, `bad_source`).
- **T3.5 `generate.py` — bounded refine loop.** propose → critique → refine,
  capped (`max_refine_attempts=2`); exhaust = blocked, recorded.
  *Acceptance:* a poisoned ask is caught and recorded, not emitted.
- **T3.6 `driver.py` — orchestrate.** `run(ask, budget) -> list[HypothesisRecord]`
  wires retrieval → decompose → propose → critique → render. Emits a population.
  *Acceptance:* smoke returns ≥3 clean records.

### Phase 4 — Firewall + pre-flight

- **T4.1 `leak_lint.py`.** *Acceptance:* flags "NQ declined 4% in March"; clean
  text passes; combined with `hypothesis.lint()` in the driver.
- **T4.2 `feasibility.py`.** `feasible(rec, dataset_caps) -> bool` maps
  `data_requires` to the subscription surface. *Acceptance:* ticks-requiring
  record is `False` on bars-only data.
- **T4.3 knowledge-window at query construction.** Search strings are *method*
  queries; instrument blacklist + knowledge horizon injected into prompts.
  *Acceptance:* test asserts prompts contain no eval-window date/current-price
  directive.

### Phase 5 — Rail handoff + provenance

- **T5.1 write to `config/hypotheses/`.** Render, then call
  `src.core.loop.hypothesis.load()` as the **final** gate, write
  `config/hypotheses/<id>.md`. *Acceptance:* files loadable by the rail; the rail
  lint is the final authority.
- **T5.2 provenance record.** Per hypothesis: sources fetched, per-pass call log
  (model_served, tokens, latency), blocked-vs-emitted → `research_log.jsonl`.
  *Acceptance:* every emitted hypothesis has a provenance row.

### Phase 6 — Orchestration loop (MVP design)

- **T6.1 `orchestrator.py`.** `run_loop(ask, max_campaigns)`:
  1. `driver.run(ask)` → hypothesis batch.
  2. For each hypothesis, invoke `scripts/nq_campaign.py` (subprocess — the
     committed four-command flow, **not** re-implemented).
  3. Collect campaign *trajectories* (whitelisted fields only).
  4. `orchestrate(trajectories) -> next_ask` — a gateway pass shown trajectories,
     never the gate.
  *Acceptance:* end-to-end with a mocked/synthetic campaign emits a `next_ask`
  and never passes gate vocab into the orchestrate prompt.
- **T6.2 safety limits.** `MAX_SOURCES_PER_HYPOTHESIS`, `MAX_TOKENS_PER_HYPOTHESIS`,
  `MAX_CAMPAIGNS_PER_LOOP`, `WALL_CLOCK` at module top; breach = blocked, never
  hangs. *Acceptance:* `budget=1` terminates and reports the bound hit.

### Phase 7 — Validation

- **T7.1 end-to-end smoke.** `driver.run(ask)` live (arXiv + gateway): ≥3
  hypotheses, both lints pass, land in `config/hypotheses/`, provenance recorded.
- **T7.2 offline determinism tests.** Frozen retrieval fixtures + mocked gateway;
  assert the *pipeline* is reproducible from fixtures (the LLM is not, stated as
  such). *Acceptance:* `tests/` pass offline.

---

## 5. The gate firewalls the orchestration loop (deferred, named)

The "try different things after the campaign" step is the forking path one level
*up* from the inner gate, and the repo says it is **not** built
(`docs/design/metric.md` Task 48; `RUNNING-A-CAMPAIGN.md` §3). So **T6.1 must not
report best-of-many as a discovery.** The orchestrator runs freely, commits
everything, claims nothing — until the main repo's L2 gate (`max_of_m`) exists.
That correction is the main repo's milestone, not silently rebuilt here.

---

## 6. Dependency tree

```
T1.1 ─ T1.2 ─ T1.3      (done)
         │
         ├─ T2.1 ─ T2.2 ─ T2.3 ─ T2.4
         │        └─ T3.1 ─ T3.2 ─ T3.3 ─ T3.4 ─ T3.5 ─ T3.6
         │                                       └─ T4.1 ─ T4.2 ─ T4.3
         │                                                         └─ T5.1 ─ T5.2
         │                                                                    └─ T6.1 ─ T6.2
         │                                                                              └─ T7.1 ─ T7.2
```

---

## 7. Open questions / notes

1. **`.env` variable name.** The key sits in `Gowtam_hypothesis/.env` under
   `Litellm=…`. The driver reads `LITELLM_API_KEY` (the MVP name). Either rename
   the variable or have the driver fall back to `Litellm`. Recommend standardizing
   to `LITELLM_API_KEY` so the same code path works for the MVP and here.
2. **General-web gap.** Ship scholarly-only now, or keep a `SearchClient` seam for
   a general key later?
3. **Models.** `claude-haiku-4-5` is the MVP's workhorse. Confirm the researcher's
   propose pass uses it and the critique pass uses a colder/cheaper deterministic
   model (README recommends "Luna" for deterministic tasks) — or same model,
   `temperature=0`.
4. **Campaign invocation.** Confirm the orchestrator drives `scripts/nq_campaign.py`
   via subprocess and reads its ledger under `runs/`.

## 8. Already scaffolded / verified

- `researcher/{__init__,schemas,render}.py` + `tests/` + `conftest.py` (Phase 1).
- **Key verified live**: `/models` (107), `claude-haiku-4-5` completion 200.
- **Renderer verified** lint-clean under `src.core.loop.hypothesis.lint`;
  all `schemas.InvalidRecord` rules reject as designed (checked by hand — `pytest`
  is not installed in this workstream).
- The `.opencode/agent/` role files from the earlier opencode draft are **superseded**
  and removed; the three passes (researcher/critic/orchestrator) are now gateway
  calls in `researcher/generate.py` + `researcher/orchestrator.py`, not agents.
