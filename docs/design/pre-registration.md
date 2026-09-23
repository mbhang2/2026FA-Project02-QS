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

---

## Amendment 2 — 2026-09-20

**The committee is four auditors, and §6 now means this concretely.**
`audit/committee/` runs **leakage, statistics, economics, robustness** over one
completed campaign, in parallel, each with its own evidence slice. Any one may
VETO; any may say CANNOT_TELL; none can approve, and `AuditorVerdict` raises on a
verdict that would mean approval.

**The isolation invariant is that no auditor sees another auditor's verdict**, not
that each sees a disjoint set of facts. Two auditors reading the same source for
different questions is two reviews; showing one the other's conclusion is how four
opinions collapse into the first one.

**Economics owns the question nobody else asks: does the code implement the
hypothesis?** The hypothesis is pre-registered and hashed into the record and the
proposing agent is shown it, but until now nothing verified the link. A campaign
whose code tests something else answered a different question from the one
registered, and that matters more than any number in the evidence.

**How much latitude the agent gets is set by how specifically the hypothesis is
written, and that is the author's choice.** This is worth knowing *before* writing
one:

- *Specific* — "an ATR-based momentum breakout strategy" names the volatility
  measure, the direction and the entry style. A fade, or a fixed point threshold
  with no volatility normalisation, is a different strategy and is vetoable.
- *General* — "an orderflow imbalance strategy" names a family and nothing else.
  Any imbalance measure, horizon or entry rule is inside it, and vetoing a
  particular choice would substitute the auditor's judgement for the author's.
- *A question* — "does an outsized move continue, or give it back?" has more than
  one correct answer. Testing either arm is a correct implementation. Only
  ignoring the premise is not.

**Drift across a campaign's trials is expected and is not a finding.** The agent
refines as it goes; that is the search working. The auditor sees the winner, and
the only question is whether the winner is still inside the guardrail.

A vague hypothesis is a weakness in the pre-registration rather than a fault in
the code, and the auditor is instructed to say so and pass rather than invent a
constraint the author did not write.

**Economics also judges whether the hypothesis is feasible at all** — a per-trade
edge smaller than spread plus commission, a horizon the fill model cannot support.
That is a minor question while a human writes hypotheses and becomes the main one
when a retrieval pipeline generates them, because a machine-proposed hypothesis
has had no sanity check before it reaches the loop.

---

## Amendment 3 — 2026-09-20 — the idle-capital charge, and why the metric was hackable

**Status: IN FORCE from commit `012c26b`, 2026-09-20.** Written while
`nq-orderflow-01` was still running and implemented after it finished. It does
**not** apply to `nq-mvp-01` or `nq-orderflow-01`: both were scored under
`MAR = 0` and stay that way, and `nq-orderflow-01` is kept deliberately as the
unconstrained arm.

Two things found during implementation are recorded below under *What
implementation changed*, because both were reward hacks inside the fix and
neither was anticipated here.

Re-scoring `nq-orderflow-01`'s 50 trials under the charge inverts the campaign:
the winner moves from trial 16 (8 exposed sessions) to trial 4 (208), and
`corr(exposed, score)` from **-0.935 to +0.762**.

### What went wrong

`MAR = 0.0` and a return vector over the full calendar make **sitting out
profitable for a losing strategy**. It is arithmetic, not a tendency:

`sortino = sum(r) / sqrt(n * sum_of_squared_losses)`, so for a fixed set of trades
`|sortino| ~ 1/sqrt(n)`. Padding the vector with flat sessions leaves the
numerator and the losses untouched and grows `n`, which drags a negative score up
toward zero. Measured on one fixed 40-session sample: **-0.0299 scored over its
own 40 sessions, -0.0097 padded to 384.** Identical trades, identical losses.

The agent optimises that score, so it learns to stop trading. `nq-mvp-01` ended on
3 trades in 384 sessions. `nq-orderflow-01`'s best trials at the time of writing
are its three least active (26, 8 and 27 sessions with exposure) while everything
trading 100+ sessions sits at -0.17 to -0.22. Brian Peterson named it on
2026-09-20: **reward hacking** — "if trading is most likely to result in a loss
(no edge), then the agent will learn to not trade."

**The gate was never fooled; the search was.** A p-value is a rank statistic, so
the common `1/sqrt(n)` factor cancels between a candidate's observed score and its
own null: measured p is **0.8846 at every padding level** while the observed score
moves from -0.2398 to -0.0774. `nq-mvp-01`'s REJECT was correct. What the padding
corrupts is which candidate the search climbs toward -- and, because `max_of_n`
picks the best by raw score, which candidate is *selected* across a campaign with
varying participation.

### The change

**A session in which the strategy held no position is charged `IDLE_CHARGE`
index points**, applied when the per-session return vector is built. Sessions
with exposure are not charged.

Two properties make this the form to use:

- **It reverses the gradient rather than flattening it.** A losing strategy now
  scores worse the less it trades (-0.2985 at 300 exposed sessions, -0.7096 at 8,
  at a charge of 0.18); a winning strategy scores better the more it trades. An
  earlier draft of this amendment proposed scoring only exposed sessions, which
  makes the score merely *indifferent* to participation. Reversing beats
  flattening, and this is Brian's "constrain or penalise the corner case".
- **Never trading scores exactly -1 for any positive charge** — 0.001 and 3.57
  give the same answer. The corner case is closed scale-invariantly, so the level
  of the charge does not decide *whether* the pathology dies, only the hurdle
  faced by the middle of the range.

### Why not a penalty term on the score

`score - lambda * (fraction not traded)` was considered and **rejected**, on two
grounds.

*It cannot work arithmetically.* The sit-out gain is proportional to the
strategy's own loss magnitude, because it comes from `1/sqrt(n)`. Going from 300
to 30 exposed sessions gains a heavy loser **+0.189** and a mild loser **+0.005**.
A fixed `lambda` is a constant subtraction and cannot cancel a per-candidate
scaling term: whichever value neutralises one over-penalises the other.

*And it has no safety guarantee.* Ng, Harada & Russell (1999), *Policy Invariance
Under Reward Transformations*, show potential-based shaping is the only additive
class that provably preserves the optimal policy — and it telescopes to
`gamma^T Phi(s_T) - Phi(s_0)`, a constant over a fixed-endpoint trajectory, so it
**cannot change which strategy wins**. That is the dilemma: a shaping term that
preserves the optimum cannot fix a problem whose symptom *is* the wrong optimum,
and one that moves the optimum is outside the guaranteed class and may reorder
legitimate candidates invisibly. The fix therefore has to change what the
objective *measures*, not add a term to it. The idle charge changes the return
series; it is not a shaping term.

Our loop is not an MDP — there is no discount factor and the score is one scalar
over a whole backtest — so the theorem does not apply literally. It is recorded
because the warning transfers: no potential-based guarantee is available to us at
this level, so no invented additive term carries one either.

### Why not synthetic losing trades

The same charge could be implemented as tagged loss rows in the blotter, with the
tag excluding them from the sign-flip. **Rejected as an implementation, not as an
idea** — the semantics above are exactly that proposal's.

The tag would be load-bearing in at least six places: `round_turns()`, commission
accounting, `trade_count`, `gross_pnl` diagnostics, blotter reconciliation, and
the economics auditor's evidence slice. Each is a silent-corruption site, and the
cost of missing one is not small: a synthetic loss that reaches the sign-flip is
flipped into a synthetic *gain*, and measured p moves from **0.0367 to 0.1813**.

None of that is needed, because **the set of sessions with exposure is invariant
under the null.** `null_from_turns` assigns each turn to `slot` from
`turn.session`; flipping a sign changes the contribution, never which session it
lands in. A flat session has no round turn to flip, so a charge keyed on "this
session had no exposure" applies identically to the observed series and to all
10,000 draws with nothing to mark and nothing to remember. The blotter stays
untouched, so `trade_count` and commission remain honest and gross-versus-net
arithmetic still reaches the economics auditor unchanged.

### What "a session with no exposure" must mean

**Not "a session with no fills", which is what `sessions_with_exposure` counts
today** (`metrics.py`, `per_session_rows`: sessions holding at least one blotter
row). That definition reopens the corner case one level down — one lot held for
one minute in every session pays no idle charge at all while the strategy is
effectively flat.

It happens to be unprofitable right now, and only just: 384 token round turns cost
**76.8** points in commission against **69.1** for never trading. The margin is
7.7 points, and it exists solely because commission per trade (0.20) exceeds the
idle charge per session (0.18). **Break-even is 0.20.** A charge set anywhere at or
above the commission rate makes token participation the cheaper option, and the
coupling between those two numbers is invisible to anyone reading either one.

So exposure is **time-weighted**: the fraction of the session during which a
position was held, with `IDLE_CHARGE * (1 - fraction)` charged against it. A
one-minute hold in a 6.5-hour session pays 0.1795 of 0.18 rather than nothing.
This closes the case structurally instead of by numerical coincidence, and it is
Brian's "minimum quote size that is not zero" in continuous form. `_holding_periods`
already exists, so the inputs are present.

### The participation floor, and what it is not for

`min_exposed_sessions` becomes a campaign setting beside N, `null_source`,
`correction` and q, written into `campaign.json` at registration so the digest
covers it — the same machinery that stops the hypothesis changing mid-campaign.

**Its job is statistical validity, not enforced activity**, and it defaults to the
existing 30. A floor is a constraint on the feasible set rather than a term in the
objective, so it is the safe kind under Ng — but it is the wrong tool for this
problem, for two reasons:

- *It pins rather than removes.* Inside the feasible region the gradient still
  points at less trading, so the agent lands on exactly the floor and stays there.
  The corner moves; it does not close.
- *It answers the question by fiat.* If absorption genuinely fires 40 times a
  year, a floor of 100 makes the real phenomenon unrepresentable and the agent
  must pad with filler to clear the bar. The campaign then tests "absorption plus
  filler", and the pre-registered question is no longer the one being answered.

Setting it high to express "a strategy trading 8 times is not worth deploying" is
legitimate, but it is a **business preference that narrows what is testable**, and
it is recorded as one rather than as a fix for reward hacking.

### The charge is bounded above as well as below

Re-scoring `nq-orderflow-01`'s trials exactly — flat sessions contributed 0, so
the new mean and downside sum follow in closed form — gives the correlation
between exposed sessions and score:

| charge | corr(exposed, score) | best trial |
|---|---|---|
| 0.0 | **-0.949** | trial 16, 8 exposed |
| 0.10 | +0.352 | trial 3, 140 exposed |
| **0.18** | **+0.622** | trial 4, 208 exposed |
| 0.30 | +0.758 | trial 4, 208 exposed |
| 1.00 | +0.960 | trial 0, **355 exposed** |

The least-active trial falls from 4th of 22 to last. But **at 1.00 the winner is
trial 0, the worst strategy in the campaign** — it lost 507 points and wins on
activity alone. That is the mirror hack: churn to dodge the charge. The charge
needs an upper bound as well as a lower one, and the bound is the same economic
argument — charging more than the true cost of capital is a thumb on the scale in
the other direction.

**The order of reasoning matters and is recorded deliberately.** 0.18 was derived
from the cost of capital *before* this table was computed. The table is a sanity
check that the honest number lands in a workable window, **not** the source of the
number. Choosing the charge because the table preferred it would be fitting a
threshold to results, which section 5 already refuses to do for the decay rule.

### Correction to an earlier claim in this amendment

An earlier draft said the corner case is closed scale-invariantly, so the level of
the charge only sets the hurdle. **That is true only at the never-trade limit.**
Across the realistic range the score becomes monotone in participation only once
the charge is comparable to the session return volatility: at a per-session
volatility of 1.0 a charge of 0.18 achieves it and 0.10 does not; at volatility
2.0 it takes 0.30. The requirement tracks volatility rather than the strategy's
loss rate, which is why a single charge can work across candidates where a fixed
`lambda` could not.

### Does the charge break the metric or the null? Measured, not argued

**The null is unaffected.** Simulating no-edge strategies (R = 300 campaigns,
B = 400 draws, against the real `score_returns`):

| | P(p<=0.05) | P(p<=0.10) | median p |
|---|---|---|---|
| no charge (today) | 0.037 | 0.080 | 0.559 |
| with idle charge | 0.043 | 0.077 | 0.544 |

Within one Monte-Carlo standard error (0.013), and both slightly conservative,
which agrees with Task 57. The reason it survives is structural: the charge is a
deterministic block identical in the observed series and in every draw, because
sign-flipping changes a turn's P&L and never which session it belongs to.

**Sortino changes meaning and should be described accordingly.** The downside
deviation now includes the idle charges, so the statistic is return per unit of
downside risk *including the cost of inaction*, not downside risk from trading
alone. For a thin strategy the idle block dominates that denominator. Intended,
but it is a different quantity from the one `nq-mvp-01` and `nq-orderflow-01`
were scored on.

**Never trading already returns REJECT, with no new code.** Every session is then
identical at `-IDLE_CHARGE`, which trips the existing zero-dispersion guard in
`score_returns`. That is stronger than the `-1` an earlier draft of this
amendment claimed.

**`MIN_DOWNSIDE_SESSIONS` effectively retires.** With `MAR = 0` every charged idle
session is below MAR, so the downside count is large for any strategy that sits
out at all and the Sharpe fallback stops firing except for a strategy that trades
nearly every session and loses in almost none. Defensible -- the fallback existed
because Sortino's denominator could rest on too few losses, and the charge
guarantees a populated denominator -- but it is a behaviour change to accept
deliberately rather than inherit.

**Nothing else moves.** The blotter is untouched, so `trade_count`, commission,
gross and net P&L and the round-turn construction are unchanged; only the
reassembled per-session vector carries the charge.

### Settled — 2026-09-20, with Michael

1. **`IDLE_CHARGE = 0.18` index points per session.** Cost of capital: about
   $20,000 initial margin at ~4.5%, over 252 days, at $20 a point
   (`20000 * 0.045 / 252 / 20 = 0.179`). Across plausible inputs -- margin
   $15k-$30k, rates 4-5% -- the derivation spans **0.12 to 0.30**, and the
   measured window in which the incentive points at participation without
   rewarding churn is **0.10 to 0.30**. 0.18 sits inside both, which is the
   claim being made: it is a reasonable economic figure that is also effective,
   not a precise one. **Frozen into `campaign.json` at registration.** If margin
   or rates move materially it is re-derived, never re-fitted to a result.
2. **Idle sessions only, not a hurdle on every session.** The narrower
   intervention: it charges "the strategy was not there", not "the strategy was
   there and underperformed". Only the first is the corner case.
3. **Exposure is time-weighted**, per the section above.
4. **The Sharpe fallback retires, and that is accepted.** It existed because
   Sortino's denominator could rest on too few losing sessions; the charge
   guarantees a populated denominator. It still fires for a strategy that trades
   nearly every session and loses in almost none, which is the case it was for.
   Reversible by raising `MIN_DOWNSIDE_SESSIONS` if that proves wrong.
5. **`min_exposed_sessions` is NOT adopted.** Considered and dropped: a floor
   leaves the gradient intact and the agent parks on it (measured: 384 -> -0.0785,
   200 -> -0.0637, 100 -> +0.0075, still monotone toward less trading), and set
   high it makes a genuinely rare phenomenon unrepresentable. The existing
   `MIN_SESSIONS = 30` stays exactly as it is.
6. **Comparability breaks at this commit.** `nq-mvp-01` and `nq-orderflow-01` were
   scored under `MAR = 0` with no charge and are not comparable to anything after
   it. A clean break, stated, not smoothed over. `nq-orderflow-01` is retained
   deliberately as the unconstrained arm: the follow-up campaign re-runs the same
   hypothesis, data, N and model with the charge on, so the two differ in one
   thing.

### Still open

- **Brian's review.** The diagnosis is his; the retirement of the Sharpe fallback
  (4) and the decision not to adopt a participation floor (5) are the two places
  where a second opinion is most worth having, since both were reasonable
  alternatives.

### What implementation changed — two hacks inside the fix

**The charge has to be per contract.** A flat charge in index points is diluted by
position size: P&L scales with contracts and a fixed charge does not, so at 100
contracts a 0.18 charge moved a score from -0.3554 to -0.0676 -- within a rounding
error of no charge at all. Sizing up escapes the idle cost, which is the same
corner case wearing a different hat. `position_scale` charges per contract of peak
position, restoring scale invariance exactly: double the position and P&L, margin
and the charge all double.

**Peak position, not largest fill.** The first fallback for blotters that never
record a non-zero position used the largest single fill, and it broke
trade-splitting invariance -- chopping one trade into two halves the largest fill
without changing the capital committed. There is no fallback now; a blotter with no
recorded position gets a scale of 1.0.

**A known bound.** The charge does not reverse the incentive for a *low-dispersion
heavy loser*, because it competes with the downside deviation and cannot outweigh a
per-session loss several times its size. Such a strategy scores near -1 and is at
the bottom of its campaign either way, so the gate is unaffected and only the
ordering among hopeless candidates is wrong. Asserted in
`tests/test_idle_charge.py` so it is a documented limit rather than a surprise.

**A second bound, found by running it (2026-09-21): the charge is absolute and the
metric is scale-invariant, so it is inert against high-turnover strategies.**

`IDLE_CHARGE` is 0.18 index points a session, which is economically right -- carry
on posted margin does not depend on how much a strategy makes. Sortino, however, is
invariant to the scale of the return series. So the charge's *relative* weight is
set by the strategy's own P&L magnitude, and that varies by two orders of magnitude
between campaigns:

| campaign | champion net | idle charge | charge as share of net |
|---|---|---|---|
| `nq-orderflow-02` | -68 pts | 36.0 pts | **53%** |
| `nq-flowcont-01` | -10,920 pts | 60.5 pts | **0.55%** |

Against `nq-orderflow-02`'s low-turnover candidates the charge dominated and the
reward hack died. Against `nq-flowcont-01`'s -- 25,000 round turns, P&L in the
thousands -- it is noise, and that campaign's `corr(sessions with exposure, score)`
was **-0.977**, the same sign and nearly the same magnitude as the unconstrained
arm's -0.935.

**That is not proof the hack returned**, because in that campaign session count and
holding period were collinear at `corr = -0.993`, so participation and horizon
cannot be separated. What is certain is that the charge was not constraining
anything there.

The synthetic fixtures behind Amendment 3 all used per-session returns of order 1.0
-- `nq-orderflow-02`'s scale -- which is why this was not found before it was
implemented. **The charge is validated in the regime it was calibrated on and
should not be assumed to hold outside it.** Whether the right response is to scale
the charge by something other than contracts, or to accept that a strategy whose
per-session P&L is thousands of points has genuinely negligible carry, is open and
is not a question to answer from one campaign.

### What this does not claim

It does not make the metric unhackable; it closes one corner case that was found
by watching an agent walk into it. `MIN_SESSIONS = 30` counts **calendar**
sessions and so is satisfied trivially by any NQ run — it has never been a
participation floor, whatever its comment implies, and this amendment does not
make it one.

## Amendment 4 — 2026-09-22 — what a trial is when the strategy is fitted

**Status: REGISTERED, NOT YET IN FORCE.** In force from the first campaign whose
profile carries `fit_sessions > 0`; nothing before that commit is affected. No
such campaign has been registered, because no ML strategy has been written -- not
because anything blocks one. The remote boundary this originally waited on was
closed on 2026-09-22 (`docs/design/ml-strategies.md` §11) and `nq-train-fitted`
has since been run against real NQ.

**This amendment needs no separate registration step.** It takes force when a
fitted campaign is registered, and `campaign.json` freezes `fit_sessions` before
trial 0 the way it freezes the budget and the q.

A fitted strategy chooses its parameters from the data. That adds three degrees
of freedom an unfitted strategy does not have -- architecture, hyperparameters,
and where the fit window ends -- and each one is a way of looking at the data.
The pre-registration has to say which looks are counted before any of them is
taken.

### 1. A trial is one scored run of the dataset

One run that produces a score is one trial, one ledger row, one look. Not the
`(architecture, hyperparameters, fit window)` triple: a triple that is never run
is not a look, and a triple run twice is two.

- Each hyperparameter configuration that is scored is its own trial.
- A refit that is scored is a new trial.
- The weights digest is recorded on the row (`weights_digest`, `weights_source`,
  `fit_sessions`) but is not itself counted. Weights are **not** in
  `ARTIFACT_GLOBS`: that set is compared before and after the run and weights are
  written mid-run, so including them would make every fitted trial invalidate
  itself.
- Within a campaign, N counts that campaign's own scored runs. Validation and
  holdout passes are governed by §5 and §8 and are unchanged by this amendment.

### 2. The fit window: `fit_sessions` = 284

Fit on sessions `[0, 284)` of the train split, score on `[284, 384)`. One hundred
scored sessions. Frozen into `campaign.json` at registration, before trial 0.

Named as the config field on purpose. `docs/design/ml-strategies.md` uses `S` for
the **scored** window (`S = 100`); an earlier draft of this amendment used `S` for
the fit window. The split itself was never in dispute -- fit 1-284, score 285-384
-- only the letter, so the letter is dropped here and `fit_sessions` is what is
registered.

Warmup is unchanged and is **before** the split; the fit window is a prefix of
the split, not of the dataset, and the scored calendar excludes it.

**The cost, recorded rather than discovered later.** One hundred scored sessions
instead of 384. Sortino's sampling error goes roughly as `1/sqrt(n)`, so the score
carries about `sqrt(384/100) = 1.96x` the noise an unfitted trial's does. That is
comfortably above `MIN_SESSIONS = 30`, but it makes the `max_of_n` bar harder to
clear honestly, and a fitted campaign should be expected to accept less often than
an unfitted one for that reason alone. It is the price of holding out a fit
window, it is accepted here, and it is **not** grounds for shrinking
`fit_sessions` after seeing a score.

### 3. Hyperparameters are trials, so the grid lives inside N

Each configuration scored spends one of the campaign's looks. At the default
`N = 50` a ten-point grid leaves five ideas. Either register a larger N for a
fitted campaign or shrink the grid. Running the grid outside the ledger and
bringing only the winner in is the failure this whole document exists to prevent.

### 4. Refitting is a trial; loading is not a refit

`load_weights` raises `FileNotFoundError` rather than quietly refitting when the
file is absent -- the message says it is "not a licence to refit". A second
`fit()` inside one run is refused by the engine. Both are already enforced in
`src/core/engine/engine.py` and covered by `tests/test_fitted_strategy.py`.

### N is campaign-scoped — restated because it was misstated

The N that enters a correction is the campaign's own registered N, default 50.
`runlog.global_n()` (772 at the time of writing) is the honesty number for
disclosure and is **not** a bar. This is already enforced, not merely intended:
`tests/test_global_n.py` asserts that nothing under `src/core/scoring/` and
nothing in `campaign.py` so much as mentions `global_n`, on the stated ground
that a bar over every trial the project ever ran would be "a bar for a family
nobody selected over, and it would grow every night whether or not this campaign
looked at anything". A fitted campaign's grid therefore competes against that
campaign's own N and does not tax any other campaign.

### What would invalidate this amendment

- Moving `fit_sessions` after seeing a score computed under it.
- Running hyperparameter configurations outside the ledger and registering only
  the survivors.
- Fitting on any session at or beyond index 284 of the train split.
- Any reading of the holdout beyond the single look in §8.
