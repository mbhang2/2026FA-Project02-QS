# Task 57 — the FWER of the mode we actually operate

**Written before anything ran.** Nothing below is adjusted in response to a
result; if the measurement contradicts an expectation stated here, the
expectation is what was wrong.

## Why

`docs/design/pre-registration.md` §3a makes the significance test a configurable
mode and says, in as many words, that **a mode labels its own claim**. The
measured family-wise error rate — **0.060, CI [0.041, 0.088]** (Task 38) — is the
*conditional* null with `max_of_n` at R = 150.

On 2026-09-19, on mentor advice, the default became **`round_turn_mc`**. Nobody
has measured what that does. So `nq-mvp-01` printed p = 0.8236 and we cannot say
what a p from that configuration means as a false-positive rate. Every MVP
campaign inherits the gap, and the project's own rule is *measure it; do not infer
it*.

## The pre-registration

**Two arms, both on edgeless synthetic data where the true answer is "nothing is
there", so every acceptance is a false positive and can be counted.**

| | arm A — paired | arm B — operating |
|---|---|---|
| campaigns | **C = 400** | **C = 400** |
| trials per campaign | **N = 10** | **N = 50** |
| candidates | Task 33's exact stream, `NullGenerator(seed=33_002)` indices 0–3999 | fresh, indices 4000–23999 |
| null source | `round_turn_mc`, **B = 10,000** | `round_turn_mc`, **B = 10,000** |
| correction | `max_of_n` | `max_of_n` |
| q | 0.05 | 0.05 |
| dataset | synthetic, 175 sessions × 390 ticks, `vol_per_tick` 0.15, `signal_strength` **0.0**, `DATASET_SEED` | same |

**Arm A is paired with Task 38 and that is the point.** Those candidate sources
are deterministic functions of the seed, so indices 0–3999 regenerate the
identical candidates with the identical observed scores. Same candidates, same
data, same N, same q, same correction — **the only thing that differs is where
each candidate's null comes from.** A difference between arm A and Task 38's
0.060 is attributable to the null source and to nothing else.

**Arm B is the configuration we actually run**, and it is the number that will be
quoted. N = 50 is the registered budget.

`--stage verify` reproduces a sample of Task 33's scores before anything else
runs, because the pairing is worth nothing if the regenerated sources differ.

## What it can resolve, stated before it runs

At C = 400 a proportion near 0.05 has a standard error of
`sqrt(0.05 × 0.95 / 400) = 0.0109`, so a 95% interval is about **±0.021**.

- It **can** distinguish 0.05 from 0.10, and 0.05 from 0.02.
- It **cannot** distinguish 0.05 from 0.06. A null result here means "no
  difference larger than about 0.03", not "no difference".
- Arm A resolves smaller differences than that, because it is paired: the test is
  **McNemar over the campaigns that flipped** between the two null sources, not a
  comparison of two confidence intervals.

## What each outcome would mean

Written now so the reading is not chosen later.

- **Near 0.05** — the cheap default controls error at its stated level, and the
  145-second null is usable for claims, not only for iteration.
- **Materially above 0.05** — `round_turn_mc` over-rejects and the default is not
  safe for a claim. The mode stays for iteration; anything claimed is re-run under
  `conditional`. This is the outcome the pre-registration already anticipates.
- **Materially below 0.05** — conservative. It costs power, keeps error control,
  and is fine to operate; the power curve would then be the thing worth measuring.

A plausible prior, recorded so it can be wrong: **above 0.05**. Holding the trade
count fixed makes a candidate's null narrower than one that re-runs the strategy
on altered data, and a narrower null is an easier bar.

## Accounting

Runs use `RunTicket.for_null_calibration`, so none of these 24,000 runs is a trial
and none touches N. They measure the apparatus; they do not search for a strategy.
