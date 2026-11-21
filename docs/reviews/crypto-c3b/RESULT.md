# C3b result: FAILED

The criteria are in [README.md](README.md), committed in `71c7bc0` before the code. Both
stages ran once, from clean commit `4cff915`, with Amendment 17's fill attribution.
**Amendment 17 was not signed at the time.** All data is simulated.

## Step 1: the selected edge

Every variant's two-sided 99% upper bound on mean session return was below zero at every
edge tested, so the rule selected the largest: **E\* = 8 bp per hour.**

| Edge (bp/hour) | Best variant's true mean (bp/session) | Largest upper bound | Campaigns whose best variant made money |
|---|---|---|---|
| 1 | -33.75 | -3.196e-03 | 0% |
| 2 | -31.56 | -2.967e-03 | 0% |
| 3 | -29.11 | -2.722e-03 | 0% |
| 4 | -26.48 | -2.461e-03 | 0% |
| 5 | -23.19 | -2.128e-03 | 0% |
| 6 | -19.79 | -1.780e-03 | 5% |
| 8 | -5.75 | -3.756e-04 | 34% |

## Step 2: the gate at E\*

| | |
|---|---|
| campaigns | 1,000 (seeds 200001 to 201000) |
| **passed the full gate** | **371** |
| pre-registered limit | 36 |
| Wilson 95% interval | 0.3416 to 0.4014 |
| rule (b) rejected | 909 |
| positive best score | 371, all of which also passed rule (b) |
| score vs no-flip draw | 5,000 trials, largest gap 1.1e-16 |

**Verdict: C3b FAIL.** The original C3 failure also stands.

## Reading it (post hoc)

This section was written after the result and does not change the verdict.

The null is not what failed. Noise was rejected in the original control, and rule (b)
correctly detects that an 8 bp edge predicts direction. The gate has no rule asking
whether expected return after costs is positive. The best 8 bp variant truly loses 5.75
bp per session, while one campaign's mean varies by about 10.9 bp, so a losing strategy
often has a winning sample. The Step 1 share of 34% is close to the 37.1% pass rate.

The simulation scores 239 sessions and the real train split 973. A longer sample should
reduce this effect. That was not tested.

## What follows

BTC campaigns stay blocked. [Amendment 18](../../design/pre-registration.md) is drafted
to address this. It is unsigned, not implemented, and needs its own control on fresh
seeds.

## Record

| file | SHA-256 |
|---|---|
| `~/crypto-controls/c3b/truth.json` | `6204bcd5fcd2dceeb3585eef7e3a177de31dc919ed833bcac744701f4c28753d` |
| `~/crypto-controls/c3b/test.json` | `b8796d231d9e8938b3c73c7919b11667f87f08993cd2daf92f32c6c9318e9a0f` |

These files are outside the repository. Both stages have been claimed and cannot be
rerun.
