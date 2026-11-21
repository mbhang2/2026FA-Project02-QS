# C3b: does the crypto gate reject a truly unprofitable edge?

**Pre-registered by the commit that adds this file, before any C3b code existed and
before either step ran.** `git log --follow --diff-filter=A docs/reviews/crypto-c3b/README.md`
gives the date.

## Why it exists

Criterion C3 of `scripts/crypto_gate_control.py` required the 95% Wilson upper bound on
the full-gate pass rate of a 5 bp edge to be at most 0.05. It observed 2 passes in 100
campaigns, an upper bound of 0.070, and **failed. That failure stays on record.** C3b
does not erase or overturn it.

C3 had two weaknesses, both found after its result was known, which is why C3b is a new
test and not a repair of the old one:

- At 100 campaigns its limit allowed **zero** passes. A gate with a true pass rate of 2%
  would have cleared it only 13% of the time.
- It assumed a 5 bp edge was unprofitable after costs and never measured that. The
  strategy can hold for several hours, so it may collect the edge more than once.

## The question

When a simulated edge is truly unprofitable after costs, does the full gate pass it no
more than 5% of the time?

## Step 1: find an edge that is truly unprofitable

| | |
|---|---|
| edges | 1, 2, 3, 4, 5, 6 and 8 bp per hour |
| seeds | 100001 to 100200: 200 campaigns per edge, used nowhere else |
| measure | each of the 5 strategy variants' mean session return after fees, funding and the idle charge, averaged per campaign |
| interval | two-sided 99% t interval across the 200 seeds, with critical value 2.601 |
| selection | **E\*** is the largest edge at which all 5 variants have an upper bound below zero |
| stop rule | if no edge qualifies, C3b stops and is reported as not run |

## Step 2: test the gate once at E\*

| | |
|---|---|
| seeds | 200001 to 201000: 1,000 fresh campaigns |
| setup | the simulator, costs and five variants of `scripts/crypto_gate_control.py`; best-of-5 `max_of_n`; B = 10,000; q = 0.05; Amendment 17's fill attribution |
| **pass** | **36 or fewer** campaigns pass the full gate (95% Wilson upper bound <= 0.05) |
| **fail** | 37 or more |

The chance C3b passes, by the gate's true pass rate at E\*:

| true rate | 2% | 3% | 4% | 5% | 6% or more |
|---|---|---|---|---|---|
| chance of passing | ~100% | 88% | 29% | 2% | ~0% |

## Rules fixed in advance

- Each step runs once from a clean commit. No added seeds, changed thresholds or
  different E\* after seeing a result.
- If C3b fails, BTC campaigns stay blocked until the cause is understood. Any new test
  needs new seeds and its own pre-registration.
- A pass clears this control on simulated data only. It says nothing about whether any
  BTC strategy has an edge.
- Whether Amendment 17 was signed when C3b ran is recorded with the result. C3b does not
  sign it and does not put it in force.
