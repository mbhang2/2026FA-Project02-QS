# The research stage — how a brief becomes a pre-registered campaign

Everything up to Task 46 assumed the hypothesis existed: a human wrote
`config/hypotheses/<name>.md`, and the campaign loop did the rest. The research
stage builds step 1 of that flow — *where the hypothesis comes from* — on the
same discipline the campaign loop already has. A human writes a research brief,
arXiv is searched, the search result is **frozen the moment it returns**,
a research agent drafts the hypothesis, and the draft is linted against the same
gate vocabulary as any committed hypothesis, because once it is registered it
*is* what the proposing agent reads.

Nothing in this stage changes how a trial is scored and nothing here can touch
the gate. `retrieve` produces a committed, hashed retrieval set; `draft`
produces a linted hypothesis; `register` bakes the brief, the retrieval and the
hypothesis digest into a write-once `CampaignRecord`; `run`/`nulls`/`report`
are then `scripts/nq_campaign.py`'s jobs, unchanged.

## 0. The pipeline at a glance

| stage | who | input | output | fixed when |
|---|---|---|---|---|
| `retrieve` | a human + `scripts/research_campaign.py` | `config/research/<name>.md`, a query | `config/retrievals/<name>.json` (query + papers + digest) | the moment the file exists — write-once |
| `draft` | the research agent (`claude-haiku-4-5`) | brief + retrieval + run facts + `docs/agent/data.md` | `runs/research/<name>/hypothesis.draft.md`, `research.draft.json` | when the human commits it |
| `register` | a human | brief + retrieval + hypothesis, **committed and clean** | `runs/campaigns/<campaign-id>/campaign.json` | write-once, like every pre-registration |
| `run`, `nulls`, `report` | `scripts/nq_campaign.py` | the record | ledger, nulls, verdict | never — outcomes are data, not settings |

The arrows are the point. Every artefact the next stage depends on is already
frozen **and hashed** by the stage before it, so the chain is: papers →
hypothesis → record, each one carrying the digest of everything it was built
from, and each one refusing to be overwritten.

## 1. The brief — a prompt, committed like a hypothesis

`config/research/<name>.md` is the human's research prompt. The arguments Task
46 made for `config/hypotheses/<name>.md` apply verbatim: it determines the
result, so it is not `docs/` prose, and its commit date is part of the
pre-registration. The brief states at minimum the question, what "there is an
effect" would look like, and what the agent is *not* being asked to do.

The brief goes **only to the research agent**. See §6 for why.

## 2. `retrieve` — the papers are a committed artefact

```
python scripts/research_campaign.py --stage retrieve --name <name> \
      [--query 'all:...'] [--max-results K] [--fulltext-papers M] [--fulltext-chars C]
```

- The query defaults to the brief's most frequent two-word collocation (headings
  excluded). That choice was measured, not guessed: arXiv treats a bare
  multi-term `all:` query as OR (noise), a long quoted phrase as an exact match
  that almost never occurs (nothing), and AND-of-generic-singletons as words
  that coincide without a shared topic (physics papers under "continuation").
  A single *specific* collocation like `all:"intraday momentum"` returned the
  most on-topic set. `--query` overrides the default, and the query is printed
  so the human sees it before it is frozen.
- `arxiv_search` hits the arXiv export API through an injectable HTTP seam
  (`_http_get`), parses the Atom feed with `xml.etree`, and fetches a plain-text
  excerpt of the top few papers from ar5iv (`fetch_fulltext`, bounded by
  `--fulltext-chars`). A paper whose full text cannot be fetched stays in the set
  abstract-only rather than being dropped.
- The result is a `RetrievalSet`: query, `retrieved_at`, `max_results`, and one
  `Paper` each (title, arxiv id, authors, categories, published, abstract,
  full-text excerpt + source). `RetrievalSet.write` refuses to overwrite an
  existing file, and `RetrievalSet.load` raises if the file's digest does not
  match its contents. A retrieval set is a commitment to a snapshot of the
  literature: change the query and you get a new name, not a corrected history.

## 3. `draft` — the research agent and the lint gate

```
python scripts/research_campaign.py --stage draft --name <name> [--preflight]
```

`ResearchAgent` (`src/core/loop/research.py`) assembles its prompt from
`config/prompts/research-agent.md` by filling, all required:

| slot | source |
|---|---|
| `{brief}` | `config/research/<name>.md` |
| `{run}` | `research_run_facts()` — dataset, fill model, commission, action cap. Facts only: never whether an edge exists, never anything the correction computes |
| `{retrieval}` | the frozen `RetrievalSet`, as a block |
| `{data}` | `docs/agent/data.md` — the whole of what a proposed strategy can act on |
| `{repair}` | empty for the first call; the rejection message on a repair call |

The prompt is split at `## USER` into system and user messages, like every other
agent prompt in this project. The template's ground rules duplicate the lint on
purpose: the questions must be about the *market*, not about how an answer would
be judged, because the output becomes the proposing agent's brief.

The output is then checked against `findings()` from `src/core/loop/hypothesis.py`
— the same gate-vocabulary scan committed hypotheses get — in a bounded repair
loop (`max_repair_attempts = 2`). Each repair call is told only *which* words
tripped the lint, shown its previous draft, and told to "restate the finding
about the MARKET, not about how an answer would be judged". If the budget is
exhausted, `draft` raises `ResearchDraftRejected` naming the findings instead of
returning a sullied hypothesis. A draft that leaks the gate is not a draft; it is
a verdict on the papers that a human should read.

Outputs live in `runs/research/<name>/`:
- `hypothesis.draft.md` — human-visible, human-installed. Nobody pushes it to
  `config/hypotheses/` but the human who reviewed it.
- `research.draft.json` — the provenance record: brief digest, retrieval digest,
  the drafting config (model, temperature, template path + digest), the lint
  findings, and the call log.

## 4. `register` — the pre-registration, papers and all

```
python scripts/research_campaign.py --stage register --name <name> \
      --campaign-id research-<name>-01
```

Register hard-fails unless the **brief, retrieval and hypothesis are all in git
and clean** (`git ls-files --error-unmatch` + `git status --porcelain`): their
commit dates *are* the pre-registration date, the same rule as the calendar.
Racing an uncommitted hypothesis to the run costs nothing here — the gate refuses
to run it.

It then loads the retrieval through `RetrievalSet.load` (tamper check) and the
hypothesis through `H.load` (lint + digest), preflights the proposing model, and
writes a `CampaignRecord` whose research fields tie the whole chain together:

- `research_prompt_path` / `research_prompt_digest` — the brief,
- `retrieval_path` / `retrieval_digest` — the papers,
- `research_config` — the research agent's drafting config (from
  `research.draft.json` when its digests match), plus a `source` block repeating
  brief and retrieval digests,

alongside the ordinary campaign fields (budget from `config/run.yaml`,
`null_source`, `correction`, q, stopping rule, agent config). The record is
written once, exactly like every pre-registration; its digest is the number the
campaign is known by.

## 5. `run`, `nulls`, `report` — the campaign itself, unchanged

These stages delegate to `scripts/nq_campaign.py` by campaign id; the record
drives them, so nothing about the evaluation is an argument to the script.

- `run` — for each trial up to `record.budget`: the proposing agent is shown
  every prior trial's score and edits the champion (`champion.py` advances only
  on a strict improvement), the harness scores the strategy over the frozen
  train split, and a ledger row plus an `agent-calls.jsonl` sidecar are written.
  Resumable: a crash mid-campaign resumes at the ledger's length.
- `nulls` — the sign-flip null over round turns per candidate, specified in
  `docs/design/pre-registration.md` §3b.
- `report` — the multiple-testing correction (`max_of_n`), the summary, and the
  tier-3 verdict, as laid out in `docs/design/metric.md`.

## 6. The prompt boundary — who sees what

| agent | sees | may not see / must not say |
|---|---|---|
| research agent | brief + papers + run facts + `docs/agent/data.md` | nothing — but its *output* is linted (§3) |
| proposing agent | the committed hypothesis + run facts only | the papers, the template, anything about the gate |

The asymmetry is deliberate and is the reason the retrieval is not read by
`llm.py`. A paper full of p-values is information the papers legitimately carry;
what must stay clean is the hypothesis, because once it lands in
`config/hypotheses/` it *is* the proposing prompt. The `src/core/loop/research.py`
docstring states it as a boundary, and `config/prompts/research-agent.md` states
it to the agent itself.

## 7. What gets hashed, where

| artefact | digest lands in |
|---|---|
| research template (`config/prompts/research-agent.md`) | `research.draft.json` → `research_config` |
| brief (`config/research/<name>.md`) | `research.draft.json`, `campaign.json` |
| retrieval (`config/retrievals/<name>.json`) | inside its own file (tamper check), `research.draft.json`, `campaign.json` |
| hypothesis (`config/hypotheses/<name>.md`) | `campaign.json` |
| entire `campaign.json` | the record digest printed at registration |

Editing any one of these after the record exists is editing a committed
pre-registration — the fix is a new name, not a rewrite of history, and the
write-once guards exist to make the mistake loud.

## 8. The live example

`research-intraday-01`, pre-registered 2026-09-22 (commit with the brief,
retrieval and hypothesis): query `all:"intraday momentum"`, four papers frozen
(`2006.08307` HMM intraday momentum, `2605.04004` MNQ momentum falsification,
`2009.04200` crypto HFT patterns, `2602.18912` overreaction/momentum), a
lint-clean draft (digest `1059d5c5…`) now at `config/hypotheses/intraday-momentum.md`,
and a record digest `efeaf697…` with budget 10.

Read next: `docs/design/pre-registration.md` for the protocol the record commits
to, `src/core/loop/research.py` for the mechanics, and `docs/tasks/task46-hypothesis-input.md`
for the design the brief slot was built to fill.

## 9. The corpus pipeline (v2) — when the literature comes first

The brief-first flow (§1–§4) assumes the question is already in someone's head.
The corpus pipeline inverts it: watch the q-fin feed, read every paper once,
throw out the ones that cannot be run or that re-derive a falsified idea, distill
one survivor into a hypothesis, and only *then* bring the pre-registration
discipline to an actual question.

```
python scripts/research_pipeline.py research --prompt "does closing pressure predict the next open?" [--query Q]
python scripts/research_pipeline.py fetch   # walk the q-fin feed, newest first
python scripts/research_pipeline.py screen  # verdict every paper without one
python scripts/research_pipeline.py rerank  # demote falsified families, score relevance
python scripts/research_pipeline.py distil  # distill the survivors into hypothesis pages
python scripts/research_pipeline.py register --arxiv-id <id> --name <name> \
      --campaign-id research-<name>-01
python scripts/research_pipeline.py view    # read the registry; --json for a dump
```

`research --prompt` is the whole chain for one question: the prompt is the lens
end to end. `fetch` derives the query from its most frequent content bigram
(`default_query`, `--query` overrides), `rerank` scores relevance against the
prompt instead of the standing list, and `distil` reads the prompt as its brief;
the run is scoped to the papers that fetch just registered, so an old feed crawl
in the same corpus does not leak into a prompt's run. Every stage is still a
first-class command — `fetch` with no prompt walks the feed for a question the
room has not asked yet.

The stages read and write the **corpus registry** (`src/core/loop/registry.py`):
one per-paper row per paper, written once — a re-fetch of a paper already seen is
a no-op even after tampering — and an append-only event log of one-shot
decisions (`screen`, `rerank`, `distil`, `register`, `outcome`). `pending` is
how each stage finds its work, and a second decision of the same kind on the
same paper raises `RegistryConflict` instead of silently updating history.

- `fetch` — the arXiv export API via the same `_http_get` seam, the q-fin
  categories newest-first, deduplicated by base id (the `vNN` suffix is not part
  of the key). `--dry-run` prints the feed without writing.
- `screen` — one verdict per paper against the **data contract** from
  `config/run.yaml`: feasibility (`run`/`direct`/`adaptable`/`infeasible`),
  a reason, an adaptation for `adaptable`, and the quote that decides it. The
  verdict is strict JSON; a fenced or prose-wrapped answer is repaired rather
  than lost, and a paper that cannot be verdict-repaired is refused loudly. The
  agent may not be asked *why* the contract cannot be disagreed with — the
  `block()` renders the facts (nq-train, 384 sessions) inside the prompt.
- `rerank` — model-free and therefore cheap: a keyword scan of title + abstract
  against the demotion families from `docs/datasets/nq-es-qqq.md` (GEX node
  attraction, sequence models at N=384, confirmation-architecture eval), with the
  citation carried in the demotion reason; and a token-overlap relevance score
  against `config/research/open-questions.md` (optional). A demoted paper is a
  corpus row all the way to the end and is never distilled.
- `distil` — one full-text read (ar5iv, bounded) per survivor, distilled into a
  hypothesis page that must pass the same `H.lint` the brief flow's drafts do
  and stay inside an 800–1,500 token budget, on the same bounded repair loop. A
  page the model cannot shape cleanly is `DistillRejected`, not a sullied
  hypothesis.
- `register` — like §4, minus the brief and the retrieval: the distilled page is
  the retrieval's replacement. It hard-fails unless the hypothesis page is
  committed and clean, preflights the proposing model, and writes the same
  `CampaignRecord` layout, with `research_type: corpus` and a `source` block
  pointing at the distill record instead of a brief and a retrieval set. The
  corpus row that produced the page gets its `register` decision, so `view`
  shows the whole chain.

The write-once rule that makes the brief flow honest applies more strongly here:
the corpus is **deliberately not in `ARTIFACT_GLOBS`**. A retrieval set is an
input artifact and is hashed into the campaign record; the corpus decides *which*
question gets asked, and the asking is still frozen as a committed hypothesis
carrying its digest. Nothing between §0 and §6 changes — `run`, `nulls` and
`report` read the same record either way.