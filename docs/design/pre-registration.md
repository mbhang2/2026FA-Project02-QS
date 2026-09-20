# Pre-registration — the MVP research protocol

**STATUS: REGISTERED** on the commit that introduced this file. Amendments below
are dated; nothing above is revised in place.

**THE REGISTRATION DATE IS THE COMMIT DATE OF THIS FILE**, exactly as for
`config/calendars/nq-trades.yaml`. There is deliberately no `registered:` field:
a date written into a file can disagree with the commit that introduced it, and
a pre-registration whose date can be wrong is not one.
`git log --follow --diff-filter=A docs/design/pre-registration.md` is the date.

Editing a decision below **after** seeing an outcome it governs is a new
pre-registration made after the fact, which is not one. Add a dated amendment
section instead; do not revise in place.

---

## 0. How to read this

Most of these were settled in conversation across a long session. Conversation is
a bad medium for this: things sound more obviously right in flow than they do
cold. **Read them cold. Push back on anything that reads differently here.** The
L2 stopping rule (§4) is the one where a subtle mistake is hardest to detect
afterwards, so read that one twice.

**Revised 2026-09-18** after a QuantStrat mentor meeting: significance testing
became a configurable mode (§3a), N moved to configuration (§3), the champion now
carries forward (§7), and the MVP treats "no veto" as approval (§6a). The first
draft argued against that last one; it is recorded as a decision with its cost
stated rather than re-argued.

Two things are still open and nothing runs until they are closed — §10.

## 1. What is being tested

An automated research loop proposes trading strategies within a pre-registered
hypothesis, scores them, and decides whether anything was found. **The
deliverable is the loop and its keep/discard gate, not a profitable strategy.**

The contribution is the multiple-testing correction over the trial ledger:
every system in the reference bibliography accepts a change when a single scalar
improves, and none corrects across hundreds of trials.

## 2. Data and cost

| | |
|---|---|
| dataset | `nq-trades`, symbol NQ |
| split for trials | **train** (384 sessions, 2024-07-05 → 2025-12-31) |
| split for the L2 gate | **validation** (86 sessions) |
| split for the final claim | **holdout** (73 sessions, 2026-06-01 → 2026-09-09) |
| fill model | `unfavorable` — a fill requires a trade strictly *through* the limit |
| commission | **0.10 index points per SIDE** |

**The commission figure and why it is per side.** NQ is $20 per index point. A
$4.00 round turn is $2.00 a side, which is 0.10 points. The engine charges
`commission_per_contract * fill.size` on *every fill*, and a round trip is two
fills — entering 0.20 here would double-charge. $4.00 is Michael's live round
turn on this contract, not an estimate.

The engine default of 0.01 is scaled for the synthetic generator's price units
and is **10x too cheap on NQ**. Left in place it would subsidise exactly the
high-turnover strategies a search drifts toward: a timing run of the baseline
momentum strategy over the train split took 206,710 fills, which at 0.10 is
20,671 points — about $413,000 — against a net of roughly -18,500 points. That
strategy is near break-even *gross* and loses entirely on costs. At the 0.01
default it would have read as a near-winner.

## 3. L1 — one campaign

| | |
|---|---|
| unit | a **trial**: one proposal, one `run.py` invocation, one ledger row |
| budget | **N, set in `config/run.yaml`** and copied into the campaign record |
| q | 0.05, when significance testing is enabled (§3a) |
| gate | the selected significance mode (§3a), on **train** |
| stopping rule | **fixed budget. No early stop on any observed result.** |

N lives in configuration rather than in this document so a campaign can be sized
to its budget without amending a pre-registration. **It is still pre-registered**:
the value is copied into `CampaignRecord` and hashed before trial 0, so a budget
edited mid-campaign fails the record's own digest check. Configuration is where
it is *written*; the hashed record is where it is *committed*.

### 3a. Two axes, not one

**A first draft of this section listed one `significance_mode` knob and that was
wrong.** `max_of_n` is a *correction*; `round_turn_mc` is a *null source*. They
are orthogonal and they compose, so the configuration has two settings.

**Axis 1 — where a candidate's null comes from** (`null_source`):

| | what it is | cost | per-candidate? |
|---|---|---|---|
| `conditional` | rotation / profile bucket at R = 150, re-running the strategy on altered data | ~15 h per campaign | **yes** |
| `round_turn_mc` | sign-flip the candidate's own round turns, B = 10,000 (§3b) | seconds | **yes** |
| `tabulated` | the committed empirical null table, via `far_for_score` | free | **no** — one strategy's null |
| `none` | no null is built | free | — |

**Axis 2 — how N candidates become one decision** (`correction`):

| | what it needs | |
|---|---|---|
| `max_of_n` | per-candidate null **samples** | the distribution of the best-of-N |
| `step_down_max` | per-candidate null **samples** | Westfall–Young |
| `bh`, `holm` | per-candidate **p-values** | false-discovery / family-wise views |
| `haircut_sharpe` | best score, N, n_sessions | closed form. **Not built** |
| `dsr` | best score, N, n_sessions | closed form. Measured biased high, +0.017 to +0.036 (Task 42) |
| `none` | nothing | read the metrics |

**Which combinations are coherent.** `max_of_n` and `step_down_max` require a
per-candidate sample source, so `null_source` must be `conditional` or
`round_turn_mc`. `bh` and `holm` require p-values, which any of the three sources
can produce. `haircut_sharpe` and `dsr` ignore axis 1 entirely — they take only
the best score and N. A configuration that asks for `max_of_n` with
`null_source: none` is incoherent and must fail at registration, not at trial 40.

**The measured error rate describes a PAIR.** FWER 0.060, CI [0.041, 0.088] is
`conditional` + `max_of_n` at R = 150 (Task 38). Change *either* axis and the
number no longer describes the procedure. A summary states both settings and what
they do and do not control.

**Two things to know before selecting `bh` or `holm` as the gate.**

*They were deliberately demoted.* Task 32 moved BH and Holm from gate to
diagnostic when the null became per-candidate and conditional: the tabulated FAR
column is **one strategy's null**, which is the assumption the conditional
construction exists to remove. Re-promoting them is allowed and is a decision, not
an oversight.

*There is a hard arithmetic ceiling on N.* The finest p-value an R-sample
empirical null can express is `1/(R+1)`, and BH's most stringent bar is `q/N`. So
BH can only resolve its own bar while **N ≤ q(R+1)**. With the committed table at
R = 2,000 and q = 0.05 that is **N ≤ 100**. At the registered N = 50 it resolves,
with roughly a factor of two of headroom; at N = 200 no result can clear a BH bar
however good it is. `resolution_cap` reports this in the summary.

### 3b. `round_turn_mc`, specified — because "Monte Carlo over round turns" is not one method

**This section exists because the phrase is ambiguous and the ambiguity is a free
parameter.** At least three different nulls answer to it, they give different
p-values, and choosing between them after seeing a result is precisely what a
pre-registration exists to prevent:

| | null it builds | destroys |
|---|---|---|
| **sign-flip** | each round turn's direction was a coin flip | whether the strategy was *right* |
| permute order | the same trades happened in a random order | *when* they happened |
| bootstrap | round turns resampled with replacement | nothing structural; measures sampling variability |

**Pre-registered method: sign-flip.** For each replication, flip the sign of each
round turn's **gross** P&L independently with probability 0.5, **re-apply
commission**, reassemble into per-session returns on the real session calendar,
and compute the same `sortino_per_session` the gate uses. **B = 10,000**
replications per candidate. The resulting sample is that candidate's
`null_samples[i]`, and `max_of_n` proceeds unchanged.

**Why sign-flip.** It preserves everything about the strategy's *activity* — how
often it traded, when, at what size, and what it paid — and destroys only whether
it was right. That is the closest analogue to "this candidate has no directional
edge", which is the thing the gate is asking about. Permutation additionally
destroys session structure, which the metric is defined over; bootstrap answers a
narrower question about sampling error.

**Why commission is re-applied rather than flipped with the P&L.** A round turn
that lost 0.5 points net paid commission to do it. Flipping the net figure would
hand the null a 0.5-point gain that never paid to trade, making the null too
generous and the gate too easy. Flip the gross, then charge the cost again.

**What this null cannot see, stated once.** It conditions on the round turns the
strategy actually took. It cannot detect a candidate that got lucky in *which*
trades it entered, because those trades are held fixed. `conditional_null` re-runs
the strategy against altered data and can. That is the difference between seconds
and hours, and it is the reason a result worth claiming is re-run under
`conditional_null` rather than claimed from this one.

**It tests directional skill, not profitability — and this needs a guard.**
Measured while implementing, 200 round turns per session over 120 sessions at
commission 0.10 a side:

| | net | observed | p |
|---|---:|---:|---:|
| no skill | −4,715 pts | −0.9423 | 0.4113 |
| slight skill, still loses to cost | **−3,275 pts** | −0.8904 | **0.0005** |

The second loses money and clears any bar. The arithmetic is right: holding the
trade count fixed makes a high-turnover candidate's null commission-dominated, so
beating it means "better than coin flips", not "profitable".

**Consequence, and it amends §5:** the `sortino > 0` floor applies at **L1 as
well as L2**. §5 first specified it only at L2, which would have let a losing
candidate clear the L1 gate and become a campaign winner. Confirmed 2026-09-19
and implemented in `summarise`: the gate now emits `accept = reject AND
observed_best > 0`, with `reject` keeping its narrow meaning of "the best of N
beat what N nulls would produce".

**If implementation reveals a problem with this specification**, that is a dated
amendment in this file, not a silent change to the method.

**The rule that makes this safe: a mode labels its own claim.** The 0.060 figure
describes `conditional_null` and nothing else. A summary produced under any other
mode says so, and says what it does and does not control. Nobody should be able
to read a `metrics_only` campaign later and quote a family-wise error rate at it.

**What the cheap modes give up, stated once so it is not rediscovered.**
`round_turn_mc` resamples the trades a strategy actually took, so it answers "given
these trades, was the sequence lucky?" — not "would this strategy have found
something in noise?". It cannot see a strategy that got lucky in *which* trades it
took. That is a real gap and it is also why it costs seconds instead of hours.
`haircut_sharpe` and `dsr` are closed forms: cheap, and carrying distributional
assumptions the resampling null does not.

**Whatever mode runs, the corrected p-value is computed and recorded when it is
affordable to do so.** A campaign run in a cheap mode still leaves the number in
the record if it can, so a claim made later has something to stand on.

**Every trial counts toward N**, including crashed ones, abandoned ones and
blocked proposals. Excluding the trials that failed is the cheapest way to shrink
a denominator and the one nobody would look for.

**A harness failure is not a trial.** If the harness fails to *start* a
strategy — a launcher that dies before the child loads — the candidate never ran
and charging it to N charges the agent for our breakage. Such rows are
superseded with a reason, never deleted.

**The agent is shown nothing about the gate.** No threshold, no p-value, no
false-acceptance rate, no N, no mention that a correction exists. It is shown the
API documents, the current strategy, the hypothesis, the run facts, and its own
prior trials with their scores. An agent shown the gate optimizes the gate.

**Nothing about validation or holdout reaches the prompt, ever.**

## 4. L2 — the loop of campaigns

**Read this section twice.**

| | |
|---|---|
| unit | a **campaign** |
| how many | **not fixed.** L2 runs until a human stops it |
| gate | the selected significance mode (§3a) over M winners, on **validation** |
| q | **0.05** — when significance testing is enabled. With `metrics_only` there is no q, and the decision is read off the metrics |

**The stopping rule is the load-bearing part.** Optional stopping biases a test
only when the stopping time is a function of the outcomes. So:

> **A human may stop L2 for any reason that is not a validation result.**

Valid: the semester ends, the compute budget is exhausted, judgement based on
**train** results that this hypothesis is exhausted. Invalid: anything informed
by how a winner did on validation.

**Which is enforced by not scoring validation during the run at all.** Every
campaign's winner is stored (sources are content-addressed). At the stop, all M
winners are scored on validation *then*, at once. This removes the temptation
rather than relying on discipline, which is this project's usual move — a human
who reads a committee report that contains a validation number has seen it,
whatever they intended.

**Campaign k+1 may depend on prior campaigns' train results — never on their
validation results.** Choosing the next hypothesis by what did well on validation
is selecting on the thing being tested.

**Why `max_of_m` rather than a Bonferroni/Holm budget over looks.** Holm is a
union bound: it assumes nothing about how the looks relate and is conservative by
construction. `max_of_m` uses the actual null distribution of the maximum, is
tighter, and is machinery whose error rate has already been measured — the
family-wise error rate is **0.060, CI [0.041, 0.088]** at R = 150 (Task 38). The
L2 gate inherits that rather than needing its own calibration study.

**One property, and one caveat.** Positive correlation between units narrows the
distribution of their maximum, so the independence assumption stays
*conservative* — it loses power and keeps error control. Campaigns under the same
hypothesis and model are positively correlated, so this carries from L1 to L2.
**The caveat:** if a future L1.5 reallocates budget across hypothesis families
based on results, campaigns become adaptively selected and this argument must be
re-checked before that exists, not after.

## 5. The pass rule at L2

Computed for each of the M winners on validation, all three reported:

| | rule | role |
|---|---|---|
| (a) | Sortino > 0 | **pass condition, at L1 AND L2.** Not a formality — see §3b, where a candidate losing 3,275 points scored p = 0.0005 against its own null |
| (b) | `max_of_m` p ≤ q | **pass condition.** The test |
| (c) | decay: validation vs train | **committee evidence, NOT a threshold** |

**(c) is deliberately not a pass condition.** "Within X%" needs an X, and this
project does not tune thresholds to make a curve look better. It would also
penalise a winner that does *better* on validation than train, which is not a
failure. Handed to the committee as evidence, it needs no constant — a human or
an auditor can weigh "train +0.42, validation +0.05" in context.

**One candidate per campaign**: the campaign's gate winner. Not top-k.

## 6. The committee, and what it cannot do

The committee reads the code and the statistics and may **veto** or **escalate to
a human**. It may **never approve.** `Verdict` has exactly three members and a
module-level assertion that none of them is named PROMOTE, ACCEPT or APPROVE;
every verdict it emits carries `can_promote: False`.

This is not fastidiousness about enums. **An auditor that can only remove
candidates is safe with a completely uncharacterised error rate** — its mistakes
cost power and can never manufacture a discovery. An auditor that can promote
must have its error rate measured first, and nobody has measured this one.

### 6a. For the MVP: no veto = approve

**Decided 2026-09-18, with mentor input.** For the MVP, a campaign that no
auditor vetoes is approved. The statistics auditor is expected to veto when a
result does not look convincing, and that is the mechanism doing the work.

**This needs no change to the verdict type**, which is worth knowing because it
makes the decision reversible. `Verdict.PASS` already means "nothing was found to
veto"; the MVP *policy* reads a PASS as approval at the campaign level. No
PROMOTE member is added, `can_promote` stays `False`, and the module-level
assertions stand. If the policy is withdrawn later, only this document changes.

**What it costs, recorded as fact rather than argument.** The error rate of
"approve when nobody vetoed" is **unmeasured**. The FWER of 0.060 describes
`max_of_n` over conditional nulls and does not describe this rule. A result
approved this way is an MVP result, and a paper claim would need either the
measured mode (§3a) or a measurement of this one.

**And the specific thing an auditor cannot see**, so it is not assumed away: two
campaigns with identical summaries — one the 1st look at validation, one the 27th
— are indistinguishable to any reviewer. "How many times have we looked" is not a
property of the campaign under review, and a lucky winner has a clean p-value, a
fine PSR and sensible code. The committee is being asked to catch unconvincing
results, which it can do; it is not being asked to catch luck-over-many-looks,
which it cannot. Where §3a's measured mode is enabled, the arithmetic still runs
alongside and is recorded.

Promotion beyond the MVP is a **human** decision, taken on all of the above, and
recorded as one.

## 7. Settled since the first draft

**1. N per campaign — now configuration.** `config/run.yaml`, copied into the
hashed campaign record. A trial on the NQ train split measured at **4.5 minutes**
(261 s, 384 sessions, 141.6M ticks of which 0.32% were stepped). So N = 60 is a
~4.5-hour run stage; under `conditional_null` the null stage adds ~15 hours;
under `round_turn_mc` it adds seconds. **A default value is still needed — see
§10.**

**2. q at L2 — 0.05**, when significance testing is enabled. With `metrics_only`
there is no q.

**3. The champion carries forward — option B.** The agent already rewrites the
whole strategy file every trial and may change anything; what changes is that the
file now *advances*. The champion becomes the base for the next proposal and
reverts on a worse result or a veto, which is Karpathy's `git reset` semantics
and what the autoquant literature does.

**The champion is campaign state, not a repository artifact.**
`src/strategies/current.py` is in `ARTIFACT_GLOBS`, so leaving a candidate in it
would change the artifact fingerprint of every subsequent run —
`loop/evaluate_step.py` notes that its `finally` "is not tidiness". So the
champion lives at `runs/campaigns/<id>/champion.py`, its digest is recorded on
every row, and `current.py` is still restored after each trial.

One consequence, in the safe direction: advancing the champion makes trials more
correlated, and positive correlation narrows the distribution of their maximum,
so `max_of_n` stays conservative. It costs power and keeps error control.

## 8. The holdout

**73 sessions. One look. Ever.**

- Opened **only if the L2 gate accepts.** If nothing survived L2, opening the
  holdout confirms nothing and spends 73 sessions that cannot be recovered.
  "We ran out of time and nothing passed, let us look anyway" is how a holdout is
  burned for zero information.
- The pass rule is written **before** it is opened.
- `HoldoutLock.unlock(reason)` is one-way, cannot be re-locked, demands a reason,
  and that reason lands in the run record. `--unlock-holdout` is on the
  agent-forbidden list.

## 9. What would invalidate this

Stated so it is not renegotiated quietly later:

- Changing the metric. Sortino on per-session returns, MAR 0, `MIN_SESSIONS = 30`.
  Changing it invalidates every calibration number in the repo.
- Changing the split calendar. Its commit date is its pre-registration.
- Changing the commission after NQ campaigns have run under it — later campaigns
  would not be comparable with earlier ones.
- Reading validation before the L2 stop, by any route including a committee
  report.
- Any look at the holdout other than the single one in §8.

## 10. Settled 2026-09-18

**Default N = 50.** In `config/run.yaml` under `campaign:`, copied into the hashed
campaign record before trial 0. A ~3.75-hour run stage on NQ train at 4.5
min/trial.

**Default significance mode = `round_turn_mc`**, specified in §3b, changeable in
the same config block. `conditional_null` is the mode a campaign is **re-run**
under when a result looks worth claiming — it is the only one whose error rate has
been measured (FWER 0.060, CI [0.041, 0.088]).

**Not yet built:** `round_turn_mc` (no trade-level bootstrap exists in the tree)
and `haircut_sharpe` (no Harvey–Liu implementation). `dsr` exists as
`expected_max_sharpe` in `audit/engine/stats.py`; `conditional_null` is what runs today.
**No campaign can run under the default mode until §3b is implemented.**

---

## Amendment 1 — 2026-09-19

**The `sortino > 0` floor applies at L1 as well as L2** (§3b, §5). Found by
implementing the sign-flip null: holding the trade count fixed makes a
high-turnover candidate's null commission-dominated, so a losing strategy can
beat it decisively. `summarise` now emits `accept = reject AND observed_best > 0`.

**The champion carries forward as the prompt's `{current}`** (§7, option B). The
implementation is narrower than the first sketch and better: `src/strategies/
current.py` is untouched and still restored after every trial, so the artifact
fingerprint is stable. What changes is only what the agent is *shown* as the
current strategy — `LLMAgent.champion` points at
`runs/campaigns/<id>/champion.py`, which advances on a strict improvement. A tie
keeps the incumbent so the base does not drift between equivalent files.

**The first registered campaign is `nq-mvp-01`**, run by `scripts/nq_campaign.py`:
N = 50, q = 0.05, `null_source: round_turn_mc`, `correction: max_of_n`, NQ train,
commission 0.10 a side, model `claude-haiku-4-5`, hypothesis
`config/hypotheses/intraday-continuation.md`.
