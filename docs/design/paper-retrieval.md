# Paper retrieval — design, not yet built

**Status: DESIGN. No code exists.** `docs/design/repo-layout.md` records paper
retrieval as loop-side work that does not exist anywhere in the tree; this is the
shape it should take when it does.

The goal: an L2 process that reads the literature, proposes a question, and hands
that question to the proposer as a pre-registered hypothesis.

---

## 1. Where it sits, and where it must not

**Between campaigns, never inside one.** A campaign's `campaign.json` records
`hypothesis_path` and `hypothesis_digest`, and every stage re-verifies the digest
before running. Retrieval that fed the proposer *during* a campaign would change
the question mid-experiment, which the digest check exists to forbid.

So the pipeline's output is **a committed hypothesis markdown file**, and the
existing `--stage register --hypothesis` takes it from there. That is the whole
interface. Nothing in `src/core/` needs to know retrieval happened.

```
   arXiv / SSRN ──▶ fetch ──▶ screen ──▶ rerank ──▶ distil ──▶ config/hypotheses/x.md
                    (0)       (1)        (2)        (3)              │
                                                                     ▼
                                                     nq_campaign.py --stage register
```

## 2. The constraint that shapes everything: a scored run may not reach the network

Scored runs are reproducible or they are nothing. An arXiv query returns different
results tomorrow, so a live query inside a trial would make the trial
irreproducible in the one way `ledger_check` cannot detect -- the code and the
artifacts would hash identically and the *inputs* would differ.

Therefore retrieval is an **offline stage that freezes its output into a committed
artifact**, exactly as `data/manifest.json` freezes the bucket. The corpus manifest
records the query, the date, and the resulting ids. A campaign cites a corpus
snapshot the way it cites a calendar.

`data/papers/manifest.json` belongs in `ARTIFACT_GLOBS` for the same reason
`config/prompts/*.md` is already there: it is an artifact that shaped the result.

## 3. The four stages

### Stage 0 — fetch (network, offline, not scored)

`scripts/fetch_papers.py`. arXiv has a real API (`export.arxiv.org/api/query`);
query `q-fin.TR`, `q-fin.ST`, `q-fin.PM`, date-bounded, rate-limited to their
requested 1 request / 3 s. Writes one JSON per paper -- id, title, abstract,
authors, date, categories -- plus extracted PDF text where a full read is wanted.

**SSRN has no public API and its terms do not permit scraping.** Treat it as a
manual-deposit path: a human drops a PDF into `data/papers/manual/` with a small
metadata sidecar. Automating it is not a technical problem to solve, it is a
licence we do not have.

Output: `data/papers/raw/<id>.json`, and `data/papers/manifest.json` recording the
query, the UTC date, and every id returned. Idempotent and resumable.

### Stage 1 — screen for feasibility against OUR data

**This is the highest-value filter and it is specific to us.** Most quant finance
papers are daily, cross-sectional, multi-asset equity studies. We have one futures
instrument, tick resolution, trades with an aggressor side, no book and no quotes,
384 intraday sessions.

`config/hypotheses/Test1.md` is the worked example of what happens without this
filter: a careful 3,147-line distillation of a real paper, requiring 15 JSE
equities ranked by ADV over a six-year daily backtest, sitting in our hypothesis
directory and testable on nothing we own.

The screen is a hard gate with a stated reason, not a score:

| question | infeasible if |
|---|---|
| instruments | needs a cross-section |
| resolution | needs daily bars only, or needs sub-tick |
| data kind | needs quotes, book depth, fundamentals, options surface |
| horizon | needs multi-year holding |
| sample | needs more sessions than the split has |

Infeasible papers are **kept with their reason**, not dropped. "Nothing discarded"
is the standing rule, and the reasons are themselves a map of what data we would
need to buy.

### Stage 2 — rerank on relevance

Only feasible papers reach it. Two signals:

- **Positive:** closeness to the open questions we have, which are the things the
  last campaigns left unresolved.
- **Negative, and hard:** `docs/datasets/nq-es-qqq.md` lists **falsified hypothesis
  families** -- GEX node attraction, sequence models at this sample size, two-stage
  confirmation architectures -- with the note that "an agent spending 200 trials
  rediscovering these is 200 trials of wasted N". Anything in a falsified family is
  demoted to the bottom with the citation, not silently dropped, so the exclusion
  is visible and arguable.

An LLM reranker with a written rubric, preflighted for model identity the way
`audit/committee` is, and its prompt committed under `config/prompts/`.

### Stage 3 — distil into a hypothesis

Produce a hypothesis markdown in **our** format, not a transcription of the paper.
It must pass `src.core.loop.hypothesis.lint` and it must be **small**: measured on
`nq-orderflow-02`, the hypothesis is ~840 tokens of a ~32,000-token prompt, about
2.6%. `Test1.md` would be ~15,300 tokens, about a third of the prompt and roughly
725,000 extra tokens over a 50-trial campaign.

**Budget the distilled hypothesis at 800-1,500 tokens.** This is the same problem
Gowtam's `spec_digest.py` solves on his branch, arrived at from the other end: he
compresses because his loop re-embeds the paper per step, we compress because the
hypothesis is the pre-registered question and a question should be short enough to
hold in mind.

The file cites the paper id and the corpus manifest digest, so the chain from
result back to source is complete.

## 4. What this costs in N — the rule that is easy to get wrong

**Retrieval, screening, reranking and distillation cost zero N.** None of them
looks at our market data. Reading a thousand papers is not a multiple-testing
problem.

**The moment a candidate hypothesis is backtested to decide whether to keep it, it
is a look, and it counts.** A pipeline that generates ten hypotheses, runs a quick
campaign on each, and registers the most promising has taken ten campaigns' worth
of looks and reported one. That is the exact failure the ledger exists to prevent,
arriving through a door marked "hypothesis selection".

So: **never screen hypotheses by running them.** Rank on relevance and feasibility,
which are properties of the paper; never on performance, which is a property of our
data. If a screening run is ever wanted, its trials go in the ledger like any other.

There is a softer version worth naming: a retrieval pipeline makes hypotheses cheap,
and campaigns are looks. Global N is 622 today. A pipeline that makes it easy to run
a campaign a day makes N grow fast, and the L2 correction is over campaigns. Cheap
hypotheses are not free.

## 5. Open decisions

1. **Who authors the hypothesis.** The pre-registration's prompt block currently
   tells the agent "a human wrote this before the campaign was registered". An
   agent-written hypothesis is still a valid pre-registration provided it is
   **frozen and committed before the campaign runs** -- that is the property that
   matters, not the authorship. But it is a change to what §1 claims and needs an
   amendment rather than a quiet edit.
2. **Whether papers are read in full or by abstract.** Abstracts are cheap and
   often enough to screen; the distillation probably needs the method section.
   A two-tier fetch (abstract for all, full text for the shortlist) is the obvious
   compromise and keeps the PDF pile small.
3. **Corpus refresh cadence.** Every refresh is a new manifest digest and therefore
   a new artifact state. Monthly is probably right; per-campaign is churn.

## 6. What this will not do

It will not find edge. A published strategy is public by construction, and the
literature's base rate on this is poor. What it can do is **propose questions worth
asking**, which is what the loop consumes -- and the gate, the correction and the
committee remain exactly as sceptical of a paper-derived hypothesis as of a
human-written one. Nothing about this pipeline touches the part of the system that
decides whether a result is real.
