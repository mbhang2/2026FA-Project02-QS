# Task 38 — the hybrid's FWER at R = 150, measured instead of corrected

**Headline: 0.0600, 95% CI [0.041, 0.088], at the resolution we actually
operate.** Task 33 measured 0.0575 at R = 50. Task 34 predicted that raising R
to 150 would bring it to about 0.047. It did not. It moved the point estimate
*up* by one campaign in four hundred, and the paired evidence says the effect is
zero.

    python docs/reviews/task38/hybrid_fwer_r150.py --stage verify
    python docs/reviews/task38/hybrid_fwer_r150.py --stage extend    # 7.1 h
    python docs/reviews/task38/hybrid_fwer_r150.py --stage report
    python docs/reviews/task38/dilution.py                           # the why

---

## 1. The measurement

Pre-registered before anything ran (commit `d14fadc`): **C = 400 campaigns x
N = 10 trials, fallback R = 150, q = 0.05**, with C chosen against a measured
throughput of 3.8 isolated runs/s rather than a guess. Same conditional
framing, same hybrid routing rule, same calibration pool, same candidates, same
q. Only R moves.

**It is paired.** Task 33's `campaigns.json` holds its 4,000 base runs, their
routing, and the 883 candidates that fell back to rotation — all deterministic
functions of the candidate source and the calibration table. So the campaigns
were not re-run: only **100 more rotations per fallback candidate**, seeded past
the existing 50 on the same 1,000-wide per-candidate block. `--stage verify`
re-ran 24 of Task 33's base candidates first and **all 24 reproduced their
scores exactly**.

| | rejects | FWER | 95% CI |
|---|---:|---:|---|
| R = 50 (Task 33's config) | 23/400 | **0.0575** | [0.039, 0.085] |
| R = 150 (what we operate) | 24/400 | **0.0600** | [0.041, 0.088] |

The R = 50 column reproduces Task 33's published 0.0575 exactly, which is the
check that the pairing is real.

**Discordant campaigns: one.** No campaign that rejected at R = 50 stopped
rejecting at R = 150; one campaign started. McNemar exact, two sided:
**p = 1.0000**.

## 2. The prediction it contradicts

Task 34 measured `max_of_n`'s resampling bias on known normal nulls, 4,000
campaigns per cell: **+0.011 at R = 50, +0.002 at R = 150**. It then restated
the hybrid headline as *"0.058 → about 0.047 **by correction, not by
construction**"*, and said plainly that the by-construction number needed a
re-run nobody had done.

The re-run says the correction does not transfer. Predicted change −0.009;
measured +0.0025.

**The rejection count is a bad instrument for this, so the p-values were
compared directly.** 23 versus 24 of 400 is one campaign; the p-value is the
continuous quantity the bias acts on, it is paired campaign by campaign, and
`max_of_n` runs at a fixed seed so its B = 10,000 resampling noise is common to
both arms:

| paired p-value, R = 50 → R = 150 | mean Δ | median Δ | rose / fell / unchanged |
|---|---:|---:|---|
| full hybrid (n = 400) | **−0.00115** | +0.00000 | 145 / 160 / 95 |
| rotation-routed trials only (n = 372) | −0.00074 | −0.00020 | 182 / 187 / 3 |

A thousandth, in the *liberal* direction, with the signs split evenly. There is
no effect of R here to correct for.

## 3. Why not — three candidate explanations, two killed

**Dilution?** Only 22.1% of trials route to rotation; the rest use profile
buckets holding 232–349 scores. A natural guess is that the buckets dominate
the simulated maximum, so moving the rotation arm from 50 to 150 is washed out.

Measured, on the same campaigns scored using **only** their rotation-routed
trials — every null then at resolution R:

| | rejects | FWER |
|---|---:|---:|
| rotation arm only, R = 50 | 38/372 | 0.1022 |
| rotation arm only, R = 150 | 41/372 | 0.1102 |

Same story, slightly larger, same direction, and the paired Δp is −0.0007.
**Not dilution.**

**Does the cap even bind?** `bias(R)` describes one specific thing: resampling
with replacement cannot produce a simulated maximum above the largest draw a
null happens to contain, so a small null truncates its own upper tail and the
test over-rejects. That bites only when the observed statistic is at or beyond
that largest draw.

| | campaigns where observed best ≥ largest null draw | median (best − largest draw) |
|---|---:|---:|
| R = 50 | **2/400 = 0.5%** | −0.2921 |
| R = 150 | **2/400 = 0.5%** | −0.3082 |

**In 398 of 400 campaigns the observed best sits below every draw in every null
the campaign has**, by a median of 0.29. The truncation the correction is about
essentially never happens here. Adding 100 rotations widened the largest draw by
0.016 in the median — against a 0.29 margin.

**So what was Task 34 measuring?** Its grid drew the nulls *and* the observed
statistic from the same normal distribution, which puts the observed maximum in
the null's tail by construction — precisely the regime where the cap binds. The
hybrid's real campaigns are not in that regime: these are edgeless candidates
whose best-of-ten lands mid-distribution, well inside nulls whose upper tails
are set by a handful of unusually lucky rotations.

`bias(R)` is not wrong. **It was applied outside the configuration it was
measured in**, and this is what that cost.

## 4. What this changes

- **The operating FWER is 0.0600, CI [0.041, 0.088].** Not 0.058, not 0.047.
  Quote the measured number.
- **The "≈ 0.047 by correction" line in `docs/design/metric.md` is withdrawn.**
  It was an extrapolation and it is contradicted by the experiment it asked for.
- **R ≥ 150 stays**, and the reason is now narrower and honest: it is cheap
  insurance in the regime where the cap *does* bind — a campaign whose best
  candidate reaches its nulls' upper tail, which is exactly what a campaign that
  found something looks like. It buys nothing measurable on known-null
  campaigns, and the R = 150 headline and the R = 50 headline are the same
  number. **Nothing was tuned in response to this**; the config stands.
- **`resolution_cap(150)` is consistent with all of it**:
  `resampling_bias 0.002`, `meets_recommended_r True`, `p_floor 0.0066`,
  `bh_max_n 7`, against `0.011` and `False` at R = 50. The cap reports the bias
  the grid measured; it does not claim that bias is realised in every
  configuration, and after this it should not be read as if it did.

## 5. Routing, cost and wall clock

| | |
|---|---|
| routing, campaigns 0–399 | bucket **77.9%** (3,117/4,000), rotation **22.1%** (883), no null 0.0% |
| rotation trials per campaign | mean 2.21, median 2; 28 campaigns have none, 19 have ≥ 5 |
| profile buckets in use | 4, holding 232 / 279 / 308 / 349 scores |
| runs added | 88,300 isolated rotations |
| wall clock | **25,655 s = 7.1 h** at **3.4 runs/s** on 8 workers |
| pre-run estimate | 6.4 h at 3.8/s on 9 workers |

The 22.1% rotation share is the stub's, and it is worth keeping next to Task
36's **98.0%** for an LLM-authored candidate set: this calibration is cheap
precisely because `NullGenerator` writes strategies the profile table already
covers. A calibration run against realistic candidates would be four times this.

## 6. What a larger C would have bought, and why it was not run

C = 400 gives se 0.0109 and the interval above. C = 800 would give se 0.0077 and
roughly [0.037, 0.068] — about a 25% narrower interval for 2.5× the compute,
with the second 400 campaigns unpaired. At the measured 3.4 runs/s that is
another 136,450 runs and about 11 hours. `--stage fresh` implements it and was
deliberately left unrun: reporting it afterwards would be a campaign count
chosen after seeing a result.

**What the current interval does and does not settle.** It separates the
operating FWER from 0.09; it does not separate 0.060 from 0.047. So this run
cannot say Task 34's corrected figure is wrong *as a value* — what it shows is
that the mechanism behind the correction is absent, the paired contrast is
indistinguishable from zero, and there is therefore no basis for reporting 0.047
rather than the measured 0.060.

## 7. Incidental: Task 33's report is no longer re-runnable as committed

`docs/reviews/task33/pool.json` was compacted after Task 33's report ran — each
row is now `{"score", "bucket"}` where it was `{"score", "returns"}` — so
`hybrid_fwer.py --stage report` raises `KeyError: 'returns'` against the
committed artifact. Nothing was lost that matters (`from_runs` used the returns
only to compute the bucket, and the bucket is what was kept), and
`profile_table()` here rebuilds the identical table from either shape. Recorded
because "the script that produced this number still runs" is a claim this
project makes, and for that one script it had quietly stopped being true.
