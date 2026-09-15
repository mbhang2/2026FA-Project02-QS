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
