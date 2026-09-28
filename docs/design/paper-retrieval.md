# Paper retrieval — stages 0-3 built, 2026-09-27

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

## 2. The query need not be reproducible; the *provenance* must be

**Decided 2026-09-20 (Michael).** An arXiv query returning different results
tomorrow is fine. What has to survive is the chain from a result back to the paper
that suggested it, and that is a *registry*, not a frozen corpus snapshot.

`data/papers/registry.json` -- one row per paper ever pulled:

| field | why |
|---|---|
| `id`, `source`, `title`, `date_pulled` | what it is and when we saw it |
| `feasibility` | `direct` / `adaptable` / `infeasible`, with the reason |
| `adaptation` | what had to change to make it testable here, if anything |
| `status` | `unread` / `screened` / `hypothesis_written` / `campaign_run` |
| `campaign_ids` | which campaigns it produced, if any |
| `outcome` | what those campaigns found |

This is **project bookkeeping, not a scored artifact**, and it deliberately does
*not* go in `ARTIFACT_GLOBS`. An earlier draft of this document put a corpus
manifest there by analogy with `config/prompts/*.md`, and the analogy is wrong: the
prompt is fingerprinted because it shapes what the agent produces *during* a
campaign. The corpus only shaped *which question was asked*, and the question is
already frozen -- as the hypothesis file, with its digest in `campaign.json`. One
frozen thing is enough.

**One caution about the `outcome` column.** A registry that records which papers
produced good results is useful -- it stops us re-testing -- and it is also a
selection surface. Choosing the next paper because papers *like* it scored well is
selection on our data, and that does touch N. Recording outcomes is right; steering
retrieval by them is a look. Keep the column, and rank on the paper's properties.

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

Output: `data/papers/raw/<id>.json`, and a row per paper appended to
`data/papers/registry.json` with the query and UTC date it came from. Idempotent
and resumable -- a paper already in the registry is not re-fetched, and its
`status` is left alone.

### Stage 1 — screen for feasibility, and for **adaptability**

**Three-way, not a binary gate** (Michael, 2026-09-20). The question is not "does
this paper's data contract match ours" but "can it be made to".

| verdict | meaning | example |
|---|---|---|
| **direct** | runs on our data as written | an intraday futures order-flow study |
| **adaptable** | runs after a stated change | equities -> futures: a technical rule on prices is instrument-agnostic. Daily bars -> we hold ticks and can aggregate to any timeframe. |
| **infeasible** | no change makes it testable | cross-sectional arbitrage, pairs, dispersion -- they need a universe and we have one instrument. Anything needing quotes, book depth, fundamentals or an options surface. |

**Adaptability has a sample-size trap.** We can build daily bars from ticks, but the
train split is 384 sessions -- so a daily strategy gets **384 observations**, and a
paper wanting a six-year daily backtest cannot run here even though the timeframe is
constructible. Screen on the *number of observations the claim needs*, not only on
whether the bars can be built. (Our metric is already per-session, so a daily
strategy maps onto it cleanly: one decision per session, 384 of them.)

**An adaptation changes the claim, and the hypothesis must say so.** Testing an
equity daily mean-reversion rule on intraday NQ futures is not testing the paper's
result. The distilled hypothesis states the adaptation explicitly so the economics
auditor can judge whether the developed strategy is still inside the guardrail --
and so nobody later reports "this paper says X works" when we tested a modified X on
different data.

Infeasible papers are **kept with their reason**. "Nothing discarded" is the standing
rule, and the reasons are a map of what data we would need to buy.

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

### Stage 2a — the second axis, added 2026-09-27

**Stage 2 as first built rated none of 224 papers `high`.** Not one paper in the
corpus was judged worth a campaign. That looked like a bad corpus and was not: it
is the scale working exactly as written.

Its first criterion is *"what would the strategy do differently on Tuesday
morning?"*, and it says a paper whose contribution is a model, an estimator, a
risk measure or a description is `low` however elegant. So these scored `low`,
correctly:

| paper | what it is |
|---|---|
| Structural Limits of OHLCV-Based Intraday Momentum Signals in MNQ Futures | a systematic falsification study on five-minute bars of the index we trade, with its own acceptance criteria — t ≥ 2.0, ≥ 30 trades per fold, positive net after friction, year stability |
| Sequential Structure in Intraday Futures Data: LSTM vs Gradient Boosting on MNQ | LSTM and GBM on MNQ 5-minute bars cannot beat the base rate out of sample, with four years of data and walk-forward validation |
| A Validated Volatility-Volume-Gap Classifier for Regime Identification in MNQ Intraday Data | a validated day classifier that says plainly it cannot support a directional strategy |

**Those are among the most useful papers in the corpus for what this project
actually builds.** The deliverable is the loop and its keep-or-discard gate, not a
profitable strategy; a second acceptance protocol on our own instrument is
evidence about ours, and a credible negative result tells us where not to spend
trials — which is what the falsified-families list in
`docs/datasets/nq-es-qqq.md` is for, and trials not spent on a known answer are
the cheapest in the project. Axis 1 is structurally blind to all of it.

**Two axes, not a re-weighted one.** `relevance` keeps its meaning exactly, so
the 224 labels already in the registry stay valid and comparable. A new
`gate_relevance` answers a different question: does this paper bear on how we
*judge* a result? A paper can be `low` on the first and `high` on the second, and
for a falsification study on NQ-like data that is the expected shape.

`gate_relevance` is raised by an acceptance protocol we can compare ours against,
a multiple-testing or overfitting method, a null construction, a measurement of
out-of-sample decay, a negative result on our instrument, or evidence about how
an automated search behaves. It is **not** raised by elegance, by machinery we do
not have, or by agreement with what we already do — a paper that would leave our
protocol identical is `low`. `high` requires naming a concrete `protocol_edit` to
the gate, the accounting or the falsified list, enforced in `rank_one` the same
way `high` relevance requires a `hypothesis_edit`.

The "do not rank by resemblance to what scored well" rule binds both axes without
exception. Retrieval and ranking cost no N precisely because they never look at
the market, and steering either axis by past scores would quietly spend it.

`scripts/rank_papers.py --rerank` adds the axis to papers scored before it
existed, and preserves a moved axis-1 label as `relevance_first_pass` rather than
overwriting it — a label that moves on re-scoring says something about the
scale's stability, and that is worth keeping.

### Stage 3 — distil into a hypothesis — **BUILT 2026-09-27**

`scripts/distil_paper.py`, governed by Amendment 8 of the pre-registration. It
writes the file and stops: registration is a human act, and the commit is the
pre-registration. Three refusals — the lint before writing, no writing while a
campaign is in flight (`config/hypotheses/*.md` is fingerprinted), and never over
an existing hypothesis.

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

The file cites the paper id and its registry row, so the chain from result back to
source is complete without the corpus itself having to be frozen.

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

## 5. Who edits the hypothesis, and when

**Decided 2026-09-20 (Michael):** a human writes the initial version; the agent
refines per trial; the agent applies new papers per campaign. That is the right
shape, and it is already *mostly* how the system works -- but "the agent edits the
hypothesis per trial" has to mean the working hypothesis, not the registered file,
or the campaign aborts.

`stage_run` re-verifies `hypothesis_digest` on every resume and exits if the file
moved. That check was added deliberately: a registered question that drifts to fit
the data is not a pre-registration.

So, three levels:

| scope | what changes | where it lives |
|---|---|---|
| **per trial** | the agent's *developed* hypothesis -- it narrows, picks a normalisation, settles on a horizon | the strategy's module docstring. Already read by the economics auditor, whose first question is whether the developed hypothesis is still inside the family the original named (Amendment 2). |
| **per campaign** | a new question, possibly from a new paper | a **new** hypothesis file, committed, registered as a new campaign |
| **never** | the registered file, while its campaign runs | refused by the digest check |

The agent already gets the latitude wanted here; it is recorded in the strategy
source rather than by rewriting the question. That is the stronger arrangement,
because at the end you can still say exactly what was registered and exactly how far
the agent moved from it -- both are on disk, and the distance between them is what
the committee judges.

**Authorship of a new campaign's hypothesis is the open part.** An agent-written
hypothesis is a valid pre-registration provided it is frozen and committed before
the campaign runs -- that property is about ordering, not authorship. But §1 of the
pre-registration currently tells the agent "a human wrote this", and that sentence
would become false. It needs an amendment, not a quiet edit.

## 6. Token budget — not about cost

The distilled hypothesis is budgeted at **800-1,500 tokens** even though tokens may
be effectively free (the gateway serves several models; local and Gemini capacity
has been mentioned). Cost was never the binding constraint. Three others are:

- **Latency.** The proposal call is part of a ~5 min/trial budget, 50 times a
  campaign. Prompt size is wall clock.
- **Context dilution.** Measured on `nq-orderflow-02`: ~32,000 prompt tokens, of
  which the hypothesis is ~840 (2.6%) and `docs/agent/strategy-api.md` is ~11,700.
  A 15,300-token hypothesis would be a third of the prompt and would crowd the
  interface reference and the champion source. Retrieval degrades with length
  regardless of what the tokens cost.
- **The pre-registration property.** A question you cannot hold in your head is a
  specification, not a registered question.

Free tokens also do not mean an unlimited **context window**, and a local model
likely has a smaller one than the gateway's hosted models. Whatever model is used
must still be preflighted and named in the record -- Task 37 lost 35 trials to a
silent fallback.

## 6. What this will not do

It will not find edge. A published strategy is public by construction, and the
literature's base rate on this is poor. What it can do is **propose questions worth
asking**, which is what the loop consumes -- and the gate, the correction and the
committee remain exactly as sceptical of a paper-derived hypothesis as of a
human-written one. Nothing about this pipeline touches the part of the system that
decides whether a result is real.
