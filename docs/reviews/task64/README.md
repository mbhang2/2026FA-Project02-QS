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
