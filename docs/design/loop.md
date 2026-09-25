# The loop, and the four decisions a real agent forced

The campaign driver: what proposes, what it is shown, how it is bounded, and
what is and is not reproducible about a run of it.

Companion to `docs/design/metric.md`, which is the gate, and
`docs/design/architecture.md`, which is the map. The split is deliberate — the
metric document says what a score means and what the correction guarantees; this
one says how candidates come to exist and what the loop does with them. Nothing
here changes the gate.

Implemented in `src/core/loop/` — `agents.py` (the stubs), `llm.py` (the model),
`campaign.py` (the driver) — and run by `scripts/campaign.py` (stub) and
`scripts/llm_campaign.py` (model, with its control arm).

---

## Four decisions, pre-registered

Task 31 deferred four things rather than deciding them by accident. Task 36
settled all four, before any code that depended on them was written, and every
one of them is in the campaign record as data so a reader does not have to take
this document's word for it.

### a. What the agent sees

**The API documents, the current strategy, its own prior trials with their
scores, and the factual conditions of the run. Nothing about the gate.**

The documents are `docs/agent/strategy-api.md`, the generated
`docs/design/context-api.md`, and the generated `docs/agent/data.md` — the same
three a human contributor is pointed at. An agent given a private, friendlier
interface is not being measured on the interface the project ships.

"Nothing about the gate" is exact, and it is the load-bearing half. No
threshold, no p-value, no false-acceptance rate, no accept or reject verdict, no
N, no correction, no mention that a correction exists. **An agent shown the
gate's output optimizes the gate. An agent shown the score optimizes the
strategy**, and the gate is then applied afterwards, over the campaign, where it
belongs. The separation is what makes the correction's guarantee about the
search rather than about a search that already knew the answer.

The prior-trial view is a **whitelist**, in `LLMAgent._history_block`: the
objective, the outcome, the trade count, the session count, and the reject
reason when there is one. Not the diagnostics, and under no circumstances
anything the correction computes. A whitelist rather than a blacklist because a
ledger row grows fields, and a blacklist leaks the first one somebody adds.

Prior scores are shown because that is what makes this research rather than
random search — and it correlates the trials, which has to be said out loud
rather than left as a worry:

> **Positive correlation between trials narrows the distribution of their
> maximum, so the independence assumption in `max_of_n` stays conservative.**
> The simulated best-of-N is built by resampling each trial's own null
> independently; if the real trials are positively dependent, the real maximum
> is stochastically *smaller* than the simulated one, so the bar the gate sets
> is too high rather than too low. The correction loses power and keeps its
> error control. The opposite direction — negatively correlated trials — would
> be the dangerous one, and an agent that is hill-climbing does not produce it.

**Nothing about validation or holdout reaches the prompt, ever.** Not the split
names, not the session counts, not the fact that a promotion path will exist.

One thing *is* added that the shipped documents do not contain: a short
statement of the run's actual conditions — which dataset, which split by name,
how many sessions, the fill model, the caps and budgets. `strategy-api.md`
describes the NQ adapter and a campaign may not be running on it; a candidate
written against the wrong instrument measures the prompt, not the agent. The
statement is facts only, and it never says whether there is an edge to find.

### b. How validation is spent

**Not at all, in Task 36. Training split only, no promotion path, no validation
looks.**

The promotion budget is a separate pre-registration and it must not be decided
as a side effect of writing the first campaign. A campaign that searched on
validation would consume the split it is eventually judged against, which is the
holdout mistake with extra steps.

### c. The stopping rule

**A fixed, pre-registered budget. No early stop on a good result, and no
extension because the last trial looked promising.**

Task 34 established that detection rises monotonically in N to at least 400, so
the rule is to pick the largest budget the bill supports, state it, and run it
out. *A campaign that stops when something looks good is a garden of forking
paths with a ledger attached* — the stopping time becomes a function of the
outcome, and every p-value computed over the campaign is then conditioned on a
rule nobody wrote down.

The rule is a field in the record (`stopping_rule`), hashed before the first
trial, and the record is re-read and re-hashed at the end.

### d. Reproducibility

**Each trial is reproducible. A campaign is not, and this document does not
pretend otherwise.**

A trial is reproducible from its stored source: `runs/sources/<digest>.py` is
content-addressed, and re-running it against the same profile reproduces the
score exactly. Task 36 demonstrated this rather than asserting it — every scored
candidate in both arms was re-run from its stored source as a side effect of
recovering session returns, and the scores are compared.

A campaign is not reproducible, because the model is not. What is recorded
instead, per campaign, is everything that determines the conditions:

| recorded | why |
|---|---|
| model id | a different model is a different experiment |
| temperature, `max_tokens`, sampling parameters | the distribution the proposals were drawn from |
| whatever seed the endpoint accepts | recorded even where the endpoint ignores it, so the absence of an effect is visible |
| endpoint | a gateway can re-point a model id |
| **prompt template digest** | the template shaped every score in the campaign |
| which documents were shown, and their paths | the interface the agent was measured on |
| `show_history`, and how much source it saw | the Task 36 arm |
| `shows_the_gate: false` | asserted in the record, not only in prose |
| retries allowed, repair bound, timeouts | the invocation discipline below |

**The prompt template is an artifact.** It lives at
`config/prompts/strategy-agent.md`, it is hashed into the campaign record, and
it is in the Task 22 artifact fingerprint set alongside the calibration tables —
so a mid-run edit invalidates the trial exactly the way a rewritten null table
does. A prompt edited between two campaigns whose results are compared is the
same defect as a null table edited between them, and it should be as loud.

---

## Invocation discipline

Two rules, both from the Task 35 patterns review, both closing the same leak.

**Bounded turns and a bounded wall clock.** A per-call HTTP timeout and a
per-proposal wall clock. Exceeding either is a blocked proposal. Never a hang.

**Silence is a failed dispatch.** No response, an empty response, an
unparseable one, a reply with no code block in it — the same event. A blocked
proposal is **a crashed trial with a ledger row that counts toward N.**

**No silent retries anywhere.** `retries_allowed: false` is in the record. An
agent that may retry until it gets a clean answer has performed trials the
ledger never saw, which is the cleanest attack on honest N there is. If retries
are ever permitted, each is a new trial with its own row, and whether they exist
at all is pre-registered rather than decided at the call site.

### How a blocked proposal becomes a row

It does not get a special outcome and it does not get special handling. The
agent returns a source file that raises at module level, naming the reason; the
trial runs; `run.py` records `outcome: crashed` with that message; the file is
stored content-addressed like any other. The alternative — returning nothing and
skipping the trial — would shrink N by exactly the proposals that failed.

### The one exception, and why it is safe

**A static check failure is not a trial.** `scripts/agent_check.py` executes
nothing: `compile()`, an AST pass and mypy, all properties of the source text.
Task 23 established that and demonstrated it — the same eighteen-probe binary
search run against it on two strategies whose peak P&L differs by 4.39 returns
identical answers, because the channel carries nothing. Iterating to repair a
syntax error therefore leaks no selection signal, and need not count.

It is bounded anyway: `max_repair_attempts` per proposal, logged separately from
N, and exhausting the bound is a blocked proposal like any other.

**A runtime failure is a trial.** That distinction is the whole of Task 23, and
it is visible in the code rather than only here: the repair loop in
`LLMAgent.propose` is driven by `src.core.checks`, which has no dataset and no
engine in it, and the trial is `run_trial`, which has both.

---

### The second exception — the synthetic probe (Amendment 7, 2026-09-25)

Off unless a campaign registers it with `--probe`. When on, a statically-clean
proposal is run once on a **synthetic** profile before its trial (`agent`, or
`harness-fitted` if it declares `fit()`), and the agent is told three things:
crashed (with the traceback tail), how many trades, and whether its fills were
byte-identical to the current strategy's. On a crash or a no-op edit it may
revise, bounded at `max_probe_repairs`. `src/core/loop/probe.py` is the whole of
it; `tests/test_probe.py` pins the three properties that make it admissible:

- **Synthetic only.** Both probe profiles name `SyntheticDataset` at
  `signal_strength: 0`. The Task 23 binary-search attack still works against
  the probe and recovers a number about seeded noise. That is why this is a
  second exception and not a reversal of the first: the static check leaks
  nothing because it executes nothing; the probe leaks nothing about NQ because
  it never touches NQ.
- **A whitelist channel.** `ProbeResult` has no field for a score or a P&L.
- **Informs, never gates.** Every exit proceeds to the trial on the last good
  file. Nothing in it can produce a blocked proposal, so the paragraph above
  this one -- a runtime failure is a trial -- is untouched. Probe rows are
  `purpose: check` in the campaign's `probe-ledger.jsonl`, never in the trial
  ledger.

What it is for: `nq-ml-02` spent two of twenty trials on edits with
byte-identical fill logs, and the fact was reproducible on synthetic data in
under a second. The design is Gowtam's worker loop -- write, run, read the
traceback, fix -- with the running moved from the model to the harness.

## The load-failure hole, found by the first real agent (Task 36)

Worth its own section because it is the exact failure this project exists to
prevent, it survived thirty-five tasks, and the thing that found it was putting
a model in the loop.

`run.py` started the strategy process in `main()`, one level outside `_walk`'s
exception handlers. A strategy that failed to **load** — a syntax error, no
`Strategy` class, anything raised at module level — therefore raised past every
handler. `LedgerRun.__exit__` still wrote a row, as it promises to, but built it
from an empty record: no `outcome`, no `error`, no score.

`CampaignResult.trials` filters on a known outcome and
`CampaignResult.harness_failures` filters on `"harness_failed"`. The row matched
neither. **It left N without leaving a trace.**

A stub that always emits valid Python never produces one. An LLM produces one on
its first malformed reply, and the rows it costs are exactly the proposals that
failed — a denominator that shrinks by its own failures.

Three layers now, in the order they fire:

1. `run.py` starts the strategy process **inside** the try. A load failure is a
   `StrategyRaised`, which the existing handler already attributes correctly:
   `outcome: crashed`, a real error string, a row that counts.
2. `CampaignResult.unaccounted` — any row that is neither a trial nor an
   admitted harness failure.
3. `summarise()` **raises** if there are any, rather than reporting a field
   nobody reads. A third category means N is wrong and every corrected p-value
   under it is computed against the wrong denominator.

The first is the fix. The second and third exist because the fix is a correct
attribution, and those are the checks that the attribution happened — "no row is
unaccounted" is a cheaper thing to assert than "every outcome is right", and a
denominator that can shrink silently is the one thing this project cannot have.

---

## What the first real campaign showed (Task 36)

Two campaigns of 100 on the synthetic training split, identical but for whether
the agent saw its own prior trials. Full numbers and the scripts:
`docs/reviews/task36/`.

**An agent shown its own scores learns, on a dataset with no edge in it, and the
gate rejects it anyway.** The history arm's scores rise with trial index —
Spearman rho +0.283, permutation p 0.0031 — and the control arm's do not (rho
−0.139, p 0.942). The two arms differ by **1.59 SD** in the mean, rank-sum
p = 0.0001, against a design that resolves 0.4 SD at 80% power. So it is not
random search with a language model attached.

What it learned is worthless, because there was nothing to learn: the profile is
`signal_strength: 0.0`. **The trajectory measures how fast this agent overfits,
not how well it researches.** Separating the two needs a planted-signal arm,
which this campaign did not have.

Every uncorrected reading of the history arm reports a discovery and the gate
does not:

| reading | verdict |
|---|---|
| best score, uncorrected | +0.4222 at a nominal false-acceptance rate of 0.0005 |
| BH over the tabulated FAR column | **97 discoveries of 99** |
| Holm over the same column | 70 discoveries |
| the DSR-shaped `deflation_view` | clears its bar by +0.4536 |
| **max-of-N over per-candidate conditional nulls** | **p = 0.686, nothing accepted** |

That gap is the project's claim, demonstrated for the first time against an
adversary that was trying.

**The rotation share was 98.0%, not the 23.5% Task 33 measured with the stub.**
The profile table is tabulated from `NullGenerator` momentum variants; an LLM
writes Donchian breakouts with ATR brackets and multi-timeframe filters, and
almost none of it lands in a covered bucket. The hybrid's fallback is not a
fallback against a real agent — it is the mechanism, and it costs 113 isolated
rotated runs per scored trial. Whether the table should be rebuilt from
LLM-authored candidates (making it a function of the model, so a new model
invalidates it) or the rotation cost is simply the standing cost of a campaign
is **not decided here**.

Two smaller results worth carrying: **198 of 198 scored trials reproduced their
score exactly** from stored source, which is decision (d) demonstrated; and
**zero repair attempts in 200 proposals** — the bounded-repair path was built,
pre-registered, and never fired. The static check is not this model's binding
constraint. The runtime is.

## Six overlapping components, and the role each has (Task 41)

The `merge-test-michael` merge brought two more loops and a second backtest
engine alongside this one. Nothing was discarded. Every component has one of
three roles — **driver** (sits on top of the harness's interface), **ported
strategy** (runs against the scored contract), or **differential reference** (a
second implementation used to check the first) — and the two that were settled by
consequence rather than preference are stated first so nobody reopens them as a
matter of taste.

### Settled by consequence

**The committed calendar is the only split.** Pre-registration cannot be applied
retroactively. If a split is computed during a run, the conditional guarantee,
the FWER measurement and every published number lose their basis at once — not
because the split would be *wrong*, but because nothing could establish it was
chosen before the outcomes were seen. `nalin_l1.splitting.windows` reads
`config/calendars/` and refuses when there is none.

The fixture registry inside `nalin/l1-loop/calendars/` exists so two toy datasets
do not re-date artifact provenance on every committed ledger row, and it is
**bounded by an enforced allowlist, not by a comment**: `FIXTURE_DATASETS` names
the two, `calendar_path` raises `FixtureCalendarRefused` for anything else, and
`audit_fixture_registry()` catches the file being added rather than only used. A
rule with nothing comparing it to the configuration has already drifted and
nobody knows yet — Task 35 found that pattern in `agent-patterns/`, and this
project has been caught by it twice more.

**One ledger.** A row asserts a `RunTicket`, a code fingerprint, artifact
digests, a dataset digest, an isolation level and a reconciled reconstruction.
That is what makes N honest and what `ledger_check` verifies. A component that
cannot emit a verifiable row **does not emit rows** — it keeps its own records,
labelled as not trials. A component that can be brought up to emitting them
should be, and that means isolation and reconstruction, not a shim writing the
same JSON shape. `Gowtam/L1/loop/evaluate_step.py` is what being brought up to it
looks like: it writes the file, invokes `run.py`, and gets a row with every one
of those fields in it.

### The four decided here

| overlap | role | why |
|---|---|---|
| **the loops** | `src/core/loop/` + `scripts/llm_campaign.py` **drives scored campaigns**; Gowtam's is a kept second driver; nalin's drives nalin's engine | Only the first has the ticket, the isolation, the ledger and the correction. Gowtam's is kept because it reaches the harness *without importing it* — a second independent driver over one interface is evidence the interface is real, and that is worth more than convenience. |
| **the `Strategy` contracts** | the event-driven, T+1-eligible one is the **scored** contract; nalin's vectorised next-bar contract is a different research object | The gate's guarantees are defined over the first. Strategies written for the second get **ported**, and a port is **not score-comparable with its source**: fractional sizing against integer contracts is a different execution model, so the two numbers are not measuring the same strategy. |
| **the fill models** | **differential reference** | Two modelling choices, not two implementations. nalin fills at the next bar's open unconditionally — a bar with no range at all still fills; the harness's `unfavorable` requires a trade strictly through the limit. They sit at opposite ends of one axis, which is what Task 4's argument about scoring on the pessimistic model is about. |
| **the metric sets** | **differential reference** | A second implementation by a different author is the strongest available check on arithmetic nobody had verified. `composite_score` is a **recorded diagnostic and never the objective** — the objective is single and pre-registered, and a scorer any code can import is what `metrics.py`'s import guard exists to stop. |

### A fourth tree, and a seventh overlap (Task 42)

`daksh/audit_agent/` arrived after the review above, on an unrelated history, and
was not covered by it. Its role: **the committee** — step 7 of the build plan,
which this document previously recorded as not existing. Frozen Python gates and
a read-only critic that cannot override a FAIL is exactly the second accept tier
the plan describes, and its gates (leak, PIT, freeze, stats) **audit empirically
what this harness enforces structurally**, which makes it a differential
reference on the guarantees themselves rather than only on arithmetic.

Two things about it change what a reader should do with its numbers.

**Its `n_trials` is a file count.** `len(glob("experiments/*.json"))` — the
experiments somebody left in a ticket directory. It is reconstructed from the
filesystem after the fact, it shrinks when a file is deleted, and it is per
ticket. This project's N is written as trials happen, includes the crashed ones,
and is **global across every dataset**. An auditor deflating by the trials it can
see is reasonable; a deflated Sharpe quoted against that number is not this
project's deflated Sharpe, and `N_TRIALS_PROVENANCE` now travels with it.

**Its DSR bar is biased high by a measured amount.** Its closed form and the
harness's simulation compute the same published quantity by different routes and
disagree by +0.017 to +0.036 in Sharpe units for N in 5–400, with the harness
closer to a high-precision reference at every N. The bias is conservative for a
gate — the bar is too high, so a real result is more likely to be called unproven
than the reverse — which is why it is recorded rather than corrected.
`docs/reviews/task42/` has the table.

Its ledger, `research/audit_db.json`, records **audits and not trials**, and says
so in the file for the same reason `nalin_l1`'s trial log does. It is written on
the first audit and has never been committed — on this branch or on the one
`audit/` arrived from — so the path is where it *will* be, not where it is.

The differential comparisons are tests, in `tests/test_teammate_integration.py`
at the **root**, because `testpaths = ["tests"]` and the teammate suites are not
collected in CI. A check that must fail when somebody gets it wrong later has to
be where CI can see it. What they found is in `docs/reviews/task41/`.

## Three tiers, and the contract between them (Task 43)

Tier 1 scores a trial (`run.py`). Tier 2 accepts or rejects a campaign, corrected
for N (`src/core/loop/`). Tier 3 audits and may only veto
(`daksh/audit_agent/tier3.py`). **Nothing imports across a tier boundary** — the
interface is `tier2-summary.json` in one direction and `tier3-verdict.json` in
the other, which is what lets three separately-authored trees be replaced
independently. `docs/design/tier-contract.md` is the schema.

Two properties are worth restating here because they bound what the gate can
claim.

**Tier 3 cannot promote, structurally.** Its verdict type has three members —
PASS, FAIL, CANNOT_TELL — and no fourth, no score and no confidence. An auditor
that can only remove candidates is safe with an uncharacterised error rate; one
that can promote is not, and nobody has characterised this one. `CANNOT_TELL` is
required and is not a veto: it escalates.

**Tier 2 owns the correction and tier 3 does not re-correct.** Applying deflated
Sharpe to a campaign of this shape is not a second opinion — it is a different
model of the null. `max_of_n` takes the maximum over N draws from N *different*
nulls, because Task 32 made the null per-candidate; the closed form assumes one.
On the Task 36 history arm the per-candidate null means run from −0.2698 to
+0.1031 against a pooled +0.0154, and the closed form's bar comes out 0.114
**below** the simulated one — below the observed best, so a pooled-null deflation
would have reported a discovery on a campaign with no signal in it.
`docs/reviews/task43/` has it.

## What the loop still does not decide

Recorded so they are not settled by accident later, the way these four nearly
were.

- **The promotion rule.** How a training-split discovery earns a validation
  look, and what that look costs against the global N.
- **Whether the agent should see anything at all across campaigns.** Everything
  above is within one campaign. A model that remembers last night's campaign is
  a different experiment and a different correction.
- **What happens when the proposing agent is not deterministic and the campaign
  is compared to another one.** Two campaigns from the same record differ, and
  nothing here quantifies by how much. One pair was run; a distribution needs
  many.
