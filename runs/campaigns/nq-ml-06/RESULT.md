# nq-ml-06 — the gate rejects. 20/20, p = 0.5244

**The first campaign in this project to complete with no plumbing failure of any
kind**: 20 trials, 20 `completed`, **0 blocked proposals, 0 truncations, 0
harness failures**, 725 minutes.

| | |
|---|---|
| proposer | `gemini-3.1-pro-preview`, 65,536-token budget |
| seed | corrected ViperQ, `8fc28c884db7d7c7` |
| N | 20, q = 0.05, `round_turn_mc` × `max_of_n`, B = 10,000 |
| exposure floor | 0.70, frozen at registration |

## The gate

| | |
|---|---|
| observed best | **+0.1280** (trial 11, 546 trades) |
| null max mean | +0.1402 |
| **p** | **0.5244** |
| reject the null | **no** |
| deflation margin | **−0.0845** (does not clear) |
| uncorrected nominal FAR of the best | 0.1681 |

**The best of 20 candidates scored BELOW what the best of 20 sign-flipped
copies of themselves averages.** Not marginal, not close: +0.1280 against
+0.1402, p = 0.52, which is the middle of the null. Every view agrees, and the
uncorrected view agrees too — even with no correction at all the best
candidate's nominal false-acceptance rate is 0.17, nowhere near the 0.001 an
uncorrected reader would need.

**This is the cleanest negative result the project has produced**, and it is
worth more than `nq-ml-03`'s accept was: nothing here needed validation or a
committee to stop it. Tier 2 stopped it on its own, on the training split, with
no look spent on validation. `docs/design/pre-registration.md` §5 — validation
is not run on a campaign the gate rejects.

## The search

| # | score | trades |
|---|---|---|
| 0 | **rejected** (67/100) | 291 |
| 1 | −0.1822 | 4,182 |
| 2 | −0.1131 | 666 |
| 3 | −0.0284 | 618 |
| 4 | −0.2048 | 534 |
| 5 | +0.1077 | 506 |
| 6 | −0.1120 | 776 |
| 7 | **rejected** (64/100) | 347 |
| 8 | −0.1956 | 966 |
| 9 | **rejected** (54/100) | 287 |
| 10 | **rejected** (64/100) | 381 |
| 11 | **+0.1280** | 546 |
| 12 | −0.0681 | 719 |
| 13 | −0.1402 | 668 |
| 14 | +0.0504 | 688 |
| 15 | −0.0693 | 704 |
| 16 | −0.0063 | 632 |
| 17 | +0.0276 | 663 |
| 18 | −0.1209 | 654 |
| 19 | −0.0642 | 532 |

16 scored, 4 rejected by Amendment 6. Four positives of sixteen, and **the
distribution is centred below zero** — median −0.0687.

### Two observations worth keeping

**The activity band replicates across proposers.** Every positive here sits
between 506 and 688 trades; trial 1's 4,182 and trial 8's 966 are the two worst
scores. `nq-ml-03`, a *different model* on the same seed and floor, put all six
of its positives between 268 and 552 trades. Two proposers converging on a
similar turnover regime is the first thing in this project that looks like a
property of the data and the cost model rather than of one search.

**The floor produces parking, as predicted.** All four rejections sit just under
it — 67, 64, 64, 54 of 100 — against `nq-ml-03`'s spread of 19 to 55.
Amendment 6 said "a cliff is what an agent parks on" as the reason for choosing
the permissive end of Michael's 70-80% range. This is that prediction being
observed: 67 of 100 is three sessions short, and a candidate one session either
side of the line is scored or not scored on a difference no reader would call
meaningful. **Recorded, not acted on** — moving a floor because a campaign
parked on it is fitting the threshold to the outcome, which is exactly what
`CLAUDE.md` forbids.

## What the campaign cost, honestly

Two predecessors were superseded to get here, both for plumbing and both
recorded as such: `nq-ml-04` (deepseek unroutable mid-campaign, 6 trials) and
`nq-ml-05` (proposer token budget too small for a model that reasons for 12k
tokens, 10 trials, 4 of them blocked). Neither was abandoned for its numbers.
This campaign is the first where the loop ran 20 clean trials end to end.
