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

---

# Result

Both arms ran to their registered size. **Zero acceptances in 800 campaigns.**

| | campaigns | accepts | FWER | 95% CI (Wilson) |
|---|---|---|---|---|
| arm A — paired, N = 10 | 400 | **0** | **0.0000** | [0.0000, 0.0095] |
| arm B — operating, N = 50 | 400 | **0** | **0.0000** | [0.0000, 0.0095] |
| pooled | 800 | **0** | **0.0000** | [0.0000, 0.0048] |
| *Task 38, conditional, R = 150* | *400* | *24* | *0.0600* | *[0.041, 0.088]* |

`0.0000` is the point estimate, not the claim. The claim the data supports is the
interval: **the operating FWER is below about 0.010, and below 0.005 pooled.**
That is the honest resolution of 800 campaigns with no events, and it is what
should be quoted.

**The floor is not doing the work.** `reject_null_only` — rejections counted
before the Amendment 1 `sortino > 0` floor is applied — is **0 in both arms**. So
this is not a case of the null passing candidates that a separate filter then
removes. The null itself never fires.

## The paired comparison

Arm A is the same 4,000 candidates, the same data, the same N, q and correction as
Task 38. The only difference is where each candidate's null comes from.

Task 38 rejected in 24 of those 400 campaigns; `round_turn_mc` rejects in 0. A zero
margin pins the 2x2 table without needing per-campaign records: 24 discordant pairs,
all in one direction, 0 concordant rejections, 376 concordant non-rejections.

**McNemar exact, two-sided: p = 1.2e-07.** The two null sources do not control
error at the same level, and the difference is not a sampling artefact.

## Reading it, against what was written beforehand

**The recorded prior was wrong, and in the opposite direction.** The pre-registration
predicted *above* 0.05, reasoning that holding the trade count fixed makes a narrower
null and therefore an easier bar. The narrowness is real; the inference from it was
not. Sign-flipping a round turn's gross P&L and re-applying commission produces a
null whose centre sits at a *positive* Sortino — near +0.18 on this data — because
every simulated variant still pays the same costs and still has its returns
concentrated in the same sessions. The bar is not easier. It is higher, and roughly
constant.

So the third branch is the one that happened: **conservative.** By the rule written
in advance, the mode keeps error control, is safe to operate, and the thing worth
measuring next is the power curve. That is Task 59, which measures it: power crosses
80% at a signal strength of 0.287, against 0.242 for the conditional null. The cost
is confined to the marginal band rather than paid everywhere, and the default stands.

**What this does not say.** It does not say `round_turn_mc` has zero false-positive
rate; 800 campaigns cannot resolve below about 0.005. It does not transfer to any
other `(null_source, correction)` pair — each mode labels its own claim, and this
one is measured only for `round_turn_mc` x `max_of_n` at q = 0.05 and B = 10,000.

## Correction to this record

The intervals above are **Wilson**, matching Task 38 and Task 59. The first version
of `fwer_round_turn_mc.py` computed a Wald interval with a `1e-12` floor inside the
standard-error square root. At 0 accepts, Wald's half-width is exactly zero, and the
floor turned an undefined interval into `[0.000, 0.0000001]` — an upper bound five
orders of magnitude tighter than 400 campaigns can buy, which would have been quoted
as near-certainty.

Corrected by recomputing from the per-campaign verdicts already stored in
`fwer.json`, not by re-running: a confidence interval is a function of
`(accepts, campaigns)` alone. Because "no re-run needed" is a claim rather than an
observation, ten campaigns were re-gated first and reproduced their stored verdicts
exactly — `accept`, `reject`, `p` and `best`, to the last digit — before anything was
rewritten. `wilson()` is checked against Task 38's published 24/400 -> [0.041, 0.088].

## Files

- `fwer_round_turn_mc.py` — the measurement; `--stage verify | runs | report`
- `fwer.json` — counts, intervals and all 800 per-campaign verdicts

The raw per-run dumps (`arm-a-runs.json` 56 MB, `arm-b-runs.json` 281 MB, and the
94 MB `_sources/` tree of generated candidates) are **not committed** — see
`.gitignore`. They regenerate deterministically from this script and the seeds it
names; `fwer.json` retains every verdict a reader needs.
