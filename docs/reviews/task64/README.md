# Task 64 — the FWER of the gate we actually operate

**Pre-registered before anything ran.** Everything below is fixed at this
commit: C, N, the seeds, the metric, the null, the correction, and — the part
that matters most here — **which numbers get reported**.

---

## 1. Why this measurement exists

`docs/design/pre-registration.md` quotes **FWER 0.060, CI [0.041, 0.088]**, and
is explicit that the figure describes a **pair**: `conditional` + `max_of_n`,
measured in Task 38 at C = 400 campaigns x N = 10 trials.

Three things have changed since, and every one of them moves a false-positive
rate:

| | Task 38 measured | what we run today |
|---|---|---|
| null source | `conditional` (rotation, R = 150) | `round_turn_mc` (sign-flip, b = 10,000) |
| metric | Sortino, `MAR = 0` | Sortino, `MAR = 0`, **plus the idle charge** (Amendment 3) |
| N | 10 | **50** |

So the error rate in the pre-registration does not describe the gate. Since the
contribution of this project is the gate and its correction, that is the central
number, not a footnote.

The overnight calibration of 2026-09-22 is related and is **not** a substitute:
it measured the **per-test** rate under the charge (0.0440 -> 0.0465, CIs
overlapping, R = 2,000). Family-wise over 50 candidates under `max_of_n` is a
different quantity.

## 2. The measurement

**C = 1,000 campaigns x N = 50 trials, q = 0.05.** At a true rate near 0.05 that
gives a Wilson interval of roughly +/-0.014, against Task 38's +/-0.024.

**Throughput, measured rather than guessed — and the first measurement of it was
wrong.** A single unloaded candidate costs **0.631 s** to run and **~1.2 s** for
b = 10,000 sign-flip resamples. Two `--stage verify` runs then took 158 s and
151 s for four campaigns, at nominal 6 and 10 workers, which looked like a wall
and implied 11 hours. It was not a wall: `pool.map` over **four** campaigns uses
four workers whatever the pool size, and CPU sat at 377% in both. Per campaign
under 4-way concurrency is therefore ~150 s, and the aggregate rate at 8 workers
is what the run stage prints every 25 campaigns rather than what this paragraph
predicts. Recorded because a throughput number nobody checked is how C gets
chosen badly.

N = 50 rather than Task 38's 10 because 50 is what `config/run.yaml` registers
and what every NQ campaign has run.

| knob | value |
|---|---|
| campaigns `C` | 1,000 |
| trials per campaign `N` | 50 |
| dataset | `SyntheticSpec(n_sessions=175, ticks_per_session=390, vol_per_tick=0.15, signal_strength=0.0)` |
| dataset seed | `4_242 + campaign_index` — **varies per campaign** |
| candidates | `NullGenerator(seed=64_001)`, one continuous stream |
| split / fill | `TRAIN`, `unfavorable`, `warmup_sessions=0` |
| metric | `score(..., idle_charge=0.18)` — the current gate's metric |
| null | `round_turn.null_sample(..., b=10_000, seed=17, idle_charge=0.18)` |
| correction | `correction.max_of_n(..., b=10_000, seed=17, q=0.05)` |
| accept | `reject AND observed_best > 0` |

**The dataset seed varies per campaign, and that is not cosmetic.** Task 38 held
one dataset fixed. If all 1,000 campaigns share data they are not independent,
and a Wilson interval — which assumes they are — would be too narrow and would
overstate what the measurement establishes. A campaign is 50 candidates on *one*
dataset; across campaigns the dataset moves.

**The real code, not a re-implementation.** The script calls
`scorer.score`, `randomization.round_turn.null_sample` and
`correction.max_of_n` — the same functions `src/core/loop/campaign.py` calls. A
faster private copy that was 1% different would measure something no campaign
runs.

## 3. What gets reported — fixed now, before the numbers exist

**Two rates, both of them, whichever way they come out.**

1. **`reject_rate`** — campaigns where `max_of_n` rejects. The correction alone.
2. **`accept_rate`** — campaigns where it rejects **and** the best observed score
   is positive. This is the gate, and this is the FWER.

Both, with Wilson intervals, and the decomposition between them.

**Why this is pinned in advance.** The idle charge means an edgeless strategy is
charged for every session it sits out, so its score is systematically negative —
the `observed_best > 0` requirement may therefore reject almost everything and
`accept_rate` may come back at or near **zero**. That would be a real and
reportable property of the gate. It would also be exactly the shape of result
somebody could manufacture by picking, after the fact, whichever of the two
numbers flattered the gate. Naming both here, before the run, is what makes the
low one credible if it appears.

A near-zero `accept_rate` is **not** evidence the gate is good on its own: a gate
that accepts nothing has no false positives and no power either. Power is the
other half and is measured separately. This document does not claim it.

Also recorded, as descriptive context and not as a test: the distribution of
`observed_best` across campaigns, and how many candidates produced an unscorable
run.

## 4. What would make this uninterpretable

- Any edit to `src/core/scoring/` while it runs. The run records
  `code_fingerprint` at start and again at the end and refuses to report if they
  differ.
- A candidate stream that is not edgeless. `--stage verify` checks that the mean
  observed score across a sample of candidates is not distinguishable from what
  an edgeless generator should produce, and prints it rather than asserting a
  threshold.
- Fewer than C completed campaigns. A partial run reports the count it actually
  has; it does not scale up.

## 5. Running it

    python docs/reviews/task64/fwer_round_turn.py --stage verify    # minutes
    python docs/reviews/task64/fwer_round_turn.py --stage run       # ~4.2 h
    python docs/reviews/task64/fwer_round_turn.py --stage report

Results land in `docs/reviews/task64/fwer.json`. The raw per-campaign records go
to the job scratch directory and are **not** committed: they regenerate from this
document's seeds, and `.gitignore` already excludes `docs/reviews/*/arm-*-runs.json`
for the same reason Task 57's 431 MB did not go into history.

---

## 6. The result, and what it turned out to mean — 2026-09-23

**FWER = 0.0020, 95% CI [0.0005, 0.0073].** C = 1,000 campaigns x N = 50 trials,
50,000 isolated runs, 5.53 h, zero unscorable trials, `code_fingerprint`
unchanged across the run.

| | |
|---|---|
| `reject_rate` (the correction) | 0.0020, CI [0.0005, 0.0073] |
| `accept_rate` (the gate) | 0.0020, CI [0.0005, 0.0073] |
| campaigns with a positive best | 852 / 1,000 |

Against a nominal `q = 0.05` the gate is about **25x conservative**. Both numbers
were fixed in `3cd6e4d` before anything ran, per section 3, and both are reported
here as promised.

### The `sortino > 0` floor is not what is doing the work

852 of 1,000 campaigns produce a positive best score, and `reject_rate` equals
`accept_rate` exactly. So every campaign the gate stops is stopped by the
**correction**, not by the profitability floor. This matters because
`round_turn.py` describes that floor as "the thing that stops" the null's
inability to see profitability. At this operating point it stops almost nothing.

### The p-values are not uniform, they are piled at 1

| percentile | 1st | 5th | 10th | 25th | 50th |
|---|---|---|---|---|---|
| p | 0.157 | 0.564 | 0.813 | 0.979 | **0.9997** |

Smallest p across 1,000 campaigns: 0.0495. A calibrated null gives roughly
uniform p-values. These say the observed best is almost always far below the
simulated maximum.

### Why — one hypothesis tested and falsified, one found

**Falsified: cross-candidate correlation.** The 50 candidates in a campaign share
a dataset, so their scores should be positively correlated, and `max_of_n`
resamples each null independently -- destroying that dependence and inflating the
simulated maximum. Measured over 12 campaigns keeping all 50 scores: **mean
pairwise correlation +0.013**, median +0.015. The effect is not there. Within-
campaign spread is 0.1671 against an across-campaign spread of means of 0.0306,
so the shared dataset explains very little.

**Found: each candidate scores below its own null.**

| | |
|---|---|
| observed mean | **-0.2869** |
| mean of its own null | **-0.0699** |
| gap | **-0.2170** |
| candidates below their own null's mean | **95.0%** (570 / 600) |
| observed std / null std | 1.32 |

The null is not too *wide* -- it is narrower than the observed. It is
mis-**centred**, sitting systematically above what the strategy achieved. That is
a sufficient explanation for p-values at 0.9997 and for a 25x conservative gate.

### The mechanism — corrected 2026-09-23

**An earlier version of this section said the cause was the fill model's spread
sitting inside `gross`, and proposed separating it per round turn. That was
wrong, and it is left corrected here rather than quietly rewritten.** Every
measurement above stands; only the attribution was mistaken.

`round_turns()` does build each excursion as

    gross += row.realized_pnl      # cost is not separated from this
    cost  += row.commission        # only commission is

and the replication is `sign * gross - cost`. But there is **no spread component
inside `gross` to separate**, because the fill model does not charge one.
`fills.py` distinguishes the two models by a single boolean:

    class FavorableFill:    strict = False   # fills if a trade occurs AT the limit or better
    class UnfavorableFill:  strict = True    # fills only if a trade occurs strictly THROUGH

Both fill a limit order at `order.limit_price` -- the same price. The unfavourable
model does not give you a worse fill; it changes **which fills happen at all**.
You still fill when price runs through your stop, and you miss fills where price
only touched your target.

So the systematic negative in `gross` is **adverse selection**, and it is not a
line item. It is a property of which round turns exist and how they resolved.
There is nothing to subtract.

**The correct statement.** The sign-flip null assumes a round turn's gross P&L is
symmetric about zero under the no-edge hypothesis. Under strict-through fills it
is not: edgeless trading has *negative* expected gross, because the fills you get
are adversely selected. Flipping a variable with a negative mean produces a
distribution centred above it, by roughly twice that mean. That is the -0.2170.

**Why the obvious repair is fatal.** Demeaning before the flip --
`sign * (g - mu) + mu - cost` -- centres the null correctly only if `mu` is pure
adverse-selection drift. It is not: `mu` also contains any real edge. A strategy
with genuine skill would have that skill preserved inside its own null, and the
test would destroy itself. An unbiased estimate of the drift has to come from
somewhere the edge cannot reach.

### What this leaves, and it is a decision rather than a patch

1. **Route anything that matters to the conditional null.** Already the
   documented design: `round_turn.py` says a result worth claiming "is re-run
   under the conditional null rather than claimed from this one." `rotation.py`
   re-runs the strategy against altered data, so adverse selection is incurred in
   both arms and the asymmetry does not arise. Hours per campaign instead of
   seconds.
2. **Estimate the drift from an edge-free reference and re-centre.** Principled,
   and the dangerous option: a reference that is wrong in the generous direction
   moves the gate toward manufacturing discoveries, which is the failure this
   project is built to prevent. It would need its own measurement before it could
   be trusted.
3. **Accept it and document the screen as a screen.** The cheap null is
   conservative; that is the price of being cheap.

This is closer to (1) than to a bug: the architecture already anticipated that
the cheap null cannot do everything, which is why the expensive one exists. What
is new is the **magnitude**. A screen running 25x conservative discards real
candidates before they ever reach the confirmatory null, and nobody had measured
that. The four NQ campaigns were all screened, none was re-run under the
conditional null, and every one of them REJECTed.

### Why it is not contradicted by the 2026-09-22 calibration

That run measured the per-test rate at 0.0465, right at nominal, and it stands.
It constructed round turns as `rng.gauss(0.0, 1.0)` -- i.i.d. and symmetric.
Sign-flipping is exactly correct for that construction by definition, so uniform
p-values were guaranteed. It measured the null's calibration **under its own
assumptions**. This run measures it against real strategy output, where the
assumption fails, and fails by 0.217.

### What this costs, and what it does not

The direction is the safe one: a null that is too generous makes the gate **harder
to pass**, so this loses power rather than manufacturing discoveries. Nothing
accepted under it is suspect.

But it does reframe evidence. Four NQ campaigns have returned REJECT, and a gate
running 25x conservative makes those rejections much weaker evidence that the
hypotheses lacked edge than they appeared to be.

### What is not in this commit

No change to the null, the correction or the metric. This is a measurement and a
diagnosis; acting on either touches the scored path, moves every existing score,
and requires deciding what happens to the four campaigns already screened under
the current null.

**The one thing already settled, 2026-09-23, with Michael: the screen is too
conservative to leave as it is.** A cheap null that discards 25 candidates for
every one a nominal gate would discard is not a screen, it is a second gate
nobody registered. What replaces it -- routing to the conditional null,
re-centring against an edge-free reference, or changing what the screen is
allowed to decide -- is the next task, and it gets its own pre-registration
before anything runs.
