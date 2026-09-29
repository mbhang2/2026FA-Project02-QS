# Research pipeline v2 — keep the brief, screen the papers

**Status: built, as a corpus-first pipeline, 2026-09-24.** After this plan was
written the user's spec overrode two of §7's rejections, so the shipped shape is:

1. **The screen, the demotion-with-citation and the append-only ledger all
   shipped** essentially as this plan describes them — but on a **corpus
   registry** (`src/core/loop/registry.py`: per-paper keys, first-wins rows,
   one-shot decisions per paper across all years), not folded into per-brief
   retrieval JSONs. `docs/design/research-loop.md` §9 documents the shipped flow.
2. **The bulk q-fin crawl and the rerank were adopted** (reversals of §7):
   `fetch` walks the q-fin feed newest-first into registry rows, and `rerank`
   demotes the falsified families plus a machine-relevance score used as a
   *ranking aid for the human*, never as the question-selection mechanism —
   §2's brief-is-the-positive-signal position is unchanged at the moment of
   `register`, where a human picks one distilled page and commits it. The screen
   survives inside the same chain (verdicts happen on the registry rows), and
   demotion reasons carry the `docs/datasets/nq-es-qqq.md` citation.
3. `scripts/research_pipeline.py` drives the whole chain with
   `fetch/screen/rerank/distil/register/view`, and `register` writes the same
   `CampaignRecord` layout as the brief flow with one frozen thing per campaign.
4. **Prompt-driven research was added** (2026-09-24, on top of the built plan):
   `research --prompt "…"` is one command for "I have a question" — the query
   derives from the prompt's most frequent content bigram (`default_query`,
   backed by v1's measured collocation rule), the prompt is the rerank's
   positive signal and the distil brief, and the run is scoped to the papers
   that fetch just registered. The brief-is-the-positive-signal line still
   holds: the prompt is the human's lens, and `register` still gates on a
   committed hypothesis.

What the plan said and what shipped diverge most visibly in §7; that table now
carries the reversals inline. The screen-analysis sections (§3–§6) are still the
contract the code implements.

## 0. Why merge at all

v1 is question-first and strongly frozen: a human brief, a small committed
retrieval set, three frozen artifacts, commit-date-as-pre-registration. Its gaps
are real: feasibility is judged *implicitly* inside the research agent's opaque
prompt; a paper whose claim we modified can later be cited as confirmed; and there
is no memory of papers from before this campaign.

The alternative's strengths are exactly the fixes for those gaps. Its weaknesses
are structural: it moves question selection from the human brief into an
LLM rank chain (a selection surface), and it makes the evidence for a registered
question a mutable, deliberately unfingerprinted registry (weaker provenance than
the three-file freeze). v2 as *planned* therefore did not adopt the crawl or the
rerank; the shipped build adopts both (status note above), keeping the rejection
alive only at the one point it matters — selection at `register` stays human.

| dimension | v1 (shipped) | corpus-first alternative | v2 (this doc) |
|---|---|---|---|
| entry point | human brief (`config/research/<name>.md`) | bulk crawl of q-fin | human brief |
| retrieval | top-K arXiv query, full text for top few | metadata crawl, PDFs only for survivors | top-K query; screen first, full text for survivors |
| feasibility | implicit in research prompt | explicit `direct/adaptable/infeasible` verdict per paper | **explicit verdict per paper, recorded inside the frozen retrieval JSON** |
| provenance | brief + retrieval + hypothesis, all committed | hypothesis only; registry mutable | brief + retrieval (incl. screen) + hypothesis |
| corpus memory | none | registry with outcome column | **append-only ledger, read by humans only** |
| falsified families | committed in `docs/datasets/nq-es-qqq.md` | negative filter at rerank | **demote with the citation, never drop silently** |
| gate | untouched | untouched | untouched |

## 1. Pipeline shape

```
human brief ──▶ retrieve ──▶ screen ──▶ freeze ──▶ draft ──▶ register
──────────────────────────────────────────────────────────────▶ run/nulls/report
                     │           │          │
                     │           │          └─ config/retrievals/<name>.json
                     │           └─ per-paper verdicts fold INTO the file above
                     └─ ledger row (append-only; no trial code reads it)
```

| stage | input | output | frozen? | selection surface? |
|---|---|---|---|---|
| retrieve | brief, `--query` | abstracts + rows | set is later frozen | yes — the query is a pick, but it is the human's |
| screen | abstracts + data contract | verdicts `direct/adaptable/infeasible` + reason | via retrieval JSON | yes, but only against the *data contract*, never the gate |
| freeze | screened set | committed retrieval JSON, tamper-checked on load | **yes** | — |
| draft | brief + frozen set + run facts | linted hypothesis | yes | yes — and deliberately a separate process from the proposing side |
| ledger | every stage above | append-only rows | no | the outcome column is one; humans only |

Frozen means write-once: a retrieval set already written refuses to be re-written,
and its digest is re-verified on `load` exactly as v1 does.

## 2. Non-negotiables carried over from v1

- **The brief is the positive signal.** Question selection stays with the human;
  LLM layers screen for feasibility, they do not pick questions. This is the
  decision that keeps v2 out of the rerank's selection problem.
- **Three-file freeze.** The commit dates of the `config/{research,retrievals,
  hypotheses}/<name>` artifacts *are* pre-registration. Nothing about the screen
  or ledger weakens this.
- **Prompt boundary.** Screen verdicts enter the research agent's context (they
  are about the data contract, not about results). They never reach the proposing
  agent's prompt, which still sees only the committed hypothesis and run facts —
  the constraint `docs/design/research-loop.md` calls "the lint gate never sees
  the gate".
- **Lint + bounded repair.** Draft output must pass `hypothesis.lint()` or be
  repaired, exactly as shipped.
- **The gate does not move.** The pre-registered calendar, the metric, block
  length, null tables and golden digests are untouched. `docs/design/pre-registration.md`
  still rules.

## 3. The screen — the one new block

Runs inside `retrieve`, after abstracts land and before any full text is fetched.

**Input per paper:** title + abstract, plus a data-contract block drawn from the
facts in `docs/agent/data.md` (instruments we hold, 384-session train split,
fill/commission model, block length). The contract is quoted, not summarized —
the screen is only as honest as its statement of what we can actually test.

**Output per paper:** exactly one of

- `direct` — testable as claimed;
- `adaptable` — testable with a stated modification; **the verdict is invalid
  without an adaptation sentence** (the code refuses it, mirroring how a draft is
  rejected at the lint gate). An adaptable verdict without text is how "we tested
  a modified X" later gets reported as "X works";
- `infeasible` — with a one-line reason.

**Hard checks inside the screen prompt:**

- *Sample-size trap.* Constructible timeframes are not testable timeframes. We can
  build daily bars but only have 384 train sessions, so a daily rule gives 384
  observations; a paper needing six years of daily data is `infeasible` even
  though the bars exist. This check is stated explicitly, because it is the subtle
  one — it fired on a live paper when exercised manually.
- *Cross-sectional impossibility.* Portfolio-selection and cross-sectional
  rankers cannot be tested on a single instrument, whatever the timeframe.
- *Contract mismatch.* Trading rules against instruments/configs outside the split
  defined in `config/calendars/nq-trades.yaml` get `adaptable` at best.

**Preflight the screen model, then parse, never default.** As in `ResearchAgent`
preflight, a silently substituted model produces verdicts that mean nothing and a
parse failure must abort the run — a bad verdict is worse than no verdict.

**Where verdicts live:** inside the frozen retrieval JSON, per paper. The frozen
set is then self-describing — the pipeline's evidence for a campaign carries its
own feasibility audit trail, and editing a verdict after freeze is the same tamper
the existing digest check already catches.

Cost: a handful of calls one level cheaper than the draft (abstract-length inputs),
tens of seconds. Not a bottleneck.

## 4. Full text after the screen, not before

v1 fetched full text for the top-K by API relevance before any judgment. v2
fetches full text only for `direct`/`adaptable` survivors, so a paper the screen
rejects costs nothing beyond its abstract — the cost discipline of the
alternative, without the crawl. If a survivor's full text cannot be fetched it
stays abstract-only, and the research agent is told so rather than left to infer
it. This is where a tier-2 fetch belongs: distillation needs the method section,
screening does not.

Consequence worth stating: the screen shapes *what the research agent reads*, and
therefore shapes the draft. That is intended and it is why the screen runs before
the freeze, and why its outputs are part of the frozen artifact.

## 5. The corpus ledger — append-only, deliberately not the freeze

A single append-only file (shape `runs/corpus-<year>.jsonl`) with one row per
paper-event: `arxiv_id`, `query`, `date_pulled`, `screen_verdict`, `adaptation`,
`status`, `hypothesis_path`, `campaign_ids`, `outcome`.

- **Written** at retrieve (the event), draft (hypothesis link), register
  (campaign id), report (outcome). Never edited in place.
- **Read** by humans and by a display command only. No trial code, no proposing
  path, no default-query code reads it. If a row is wrong, a new row records it;
  this is deliberate and matches the registry-idempotency argument: a re-run is
  not a reason to forget a decision.
- **Not fingerprinted, and not in `src/core/scoring/integrity.py`'s artifact
  globs.** Restating why: the prompt fingerprint exists because the prompt shapes
  what an agent produces *during* a campaign. The corpus only shaped which
  question got asked, and the question is already frozen as the committed
  hypothesis with its digest in the campaign record. One frozen thing per
  campaign is enough — and the ledger's append-only mutability cannot invalidate
  a running trial, whereas a fingerprint on it would be a check run against
  moving history.
- **The outcome column is a selection surface, and the rule is: it must never
  steer retrieval.** Recording which papers produced good results is right and
  stops re-testing (that is what the committed falsified-families list already
  does, statically). Feeding those outcomes back into the default query is a look
  at our own data and would corrupt the next campaign's pre-registration. The
  ledger exists to answer "have we seen this, and what happened", not "what
  should we look at".

## 6. Falsified families: demote with the citation, not drop

The screen cross-references the committed list in `docs/datasets/nq-es-qqq.md`
("Falsified hypothesis families — do not re-explore": GEX node attraction,
sequence models at this sample size, two-stage confirmation). A paper in a
falsified family is kept in the set with a `demoted` flag and the citation
attached, not silently deleted — the exclusion stays arguable and auditable. If a
human wants it anyway, `--query` overrides and the flag stays visible. This is
the alternative's negative filter, wired to a prior that is already committed.

## 7. What this rejects, and why

| alternative idea | rejection |
|---|---|---|
| bulk q-fin crawl as the default entry | scope change, and data is the binding constraint, not candidate supply. Retrieval is per-brief; the corpus can grow later via the ledger's write path — **adopted 2026-09-24 as `fetch`: the pipeline's default entry is now the feed list, but question selection at `register` stays human and manual** |
| rerank / "closeness to open questions" positive signal | the signal is either the human brief (already exists, committed) or outcome-derived retrieval (selection on our data). Both are worse than the brief — **adopted 2026-09-24 as a human-facing rank aid** (token-overlap relevance + family demotion printed for the human to read); it never auto-selects a paper for registration |
| registry as the provenance source | mutable and unfingerprinted by design — reconstructed provenance for a registered hypothesis. The three-file freeze keeps the evidence reproducible from a commit alone — **stands**; register still freezes the one chosen page + record, and the per-paper rows are write-once |
| fingerprinting the ledger | falsifies the ledger's own purpose as running history — **stands**; the registry is not in `ARTIFACT_GLOBS` |

## 8. Build order

Each step is independently useful, keeps `tests/test_doc_citations.py` green, and
does not change the shape of the register/run/null/report half.

1. **Screen sub-step in `retrieve`.** Add `screen()` to `src/core/loop/research.py`
   (contract block + verdict parse + `adaptable`-requires-text + sample-size
   check), run it before full-text fetch, fold verdicts into the retrieval JSON as
   an optional `screen` block. Old sets without the block load unchanged — the
   field is additive.
2. **Full-text-after-screen.** Move the `fulltext_papers` decision in
   `scripts/research_campaign.py` to post-screen survivors.
3. **Ledger writes + view.** Append rows at the four points; add a display
   subcommand to `scripts/research_campaign.py`. No pipeline reads it.
4. **Demotion flag.** Parse the falsified-families list from
   `docs/datasets/nq-es-qqq.md`, attach `demoted` + citation at screen time.

## 9. Tests that must hold

- `tests/test_research.py` extended: verdict parse; `adaptable` without text is
  refused; sample-size trap fires on a fixture paper; screen preflight aborts on
  model mismatch; retrieval JSON with/without the `screen` block both load and
  tamper-check.
- `tests/test_doc_citations.py`: clean — new paths are cited as placeholders or
  only after they exist; the ledger shape token above is a shape, not a path.
- Ledger is provably outside the artifact globs and outside the proposing prompt
  (assert in `tests/test_research.py` or the integrity test).

## 10. Open questions

- Verdict verbosity budget — a screen block that bloats the research prompt
  shrinks the interface budget; cap at one line + the adaptation sentence.
- Who re-screens a paper that was `infeasible` the first time if the data contract
  changes (e.g. a second instrument is added)? Proposal: the ledger row stays; the
  new contract is itself a new fetch event.
- Whether the `demoted` flag should also throttle re-pulling of the same paper
  across campaigns, or only annotate it. Default here: annotate only.