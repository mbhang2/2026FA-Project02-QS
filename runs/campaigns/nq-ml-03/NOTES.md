# nq-ml-03 — running notes

Written while the campaign runs. Observations only; nothing here changes the
campaign, and no code under `src/core/` or `run.py` is touched while it is in
flight (both are inside `code_fingerprint`).

## Trial-by-trial

| # | outcome | trades | exposed | note |
|---|---|---|---|---|
| 0 | scored −0.1030 (uncharged −0.0973) | 1,732 | 100/100 | the seed's own shape: trades most days |
| 1 | **rejected** | 134 | 48/100 | **Amendment 6's first bite.** Under nq-ml-02's rules this would have been scored |
| 2 | crashed (blocked proposal) | — | — | static check unrepaired after 2 attempts |

## What trial 2 was

Not a strategy fault and not a harness fault: the model returned a file that
failed the static check twice in a row, so `LLMAgent.propose` returned the
blocked-proposal module, `run.py` recorded `crashed`, and the row counts toward
N. That is the pre-registered behaviour (`loop.md`, "How a blocked proposal
becomes a row") and the alternative — skipping the trial — would shrink N by
exactly the proposals that failed.

The specific failure is **numpy-versus-mypy friction**, not strategy logic:

```
error: Incompatible types in assignment
(expression has type "floating[Any]", variable has type "float")  [assignment]
```

A numpy reduction returns `np.floating`, the model annotated the target `float`,
and mypy rejects the assignment. This is the same family that made `nq-ml-01`'s
seed fail `_static_ok` at registration (module-level `len()`, `None`-init, an
un-narrowed `edge`). ViperQ is numpy-heavy, so it can recur.

**Watch the rate, do not act on it.** `nq-ml-01` lost 13 of 20 this way. If this
campaign does the same the result is still an honest campaign — N counts blocked
proposals — but the search will have had few real looks. Stopping and
re-registering *because early trials looked bad* is the forking path the
correction does not cover, so it is not a call to make mid-flight.

**The fix, for after the campaign:** the repair prompt can name the pattern
(`float(np.mean(...))` at the assignment) without naming anything about the
gate. The prompt template is an artifact hashed into the campaign record, so
that edit belongs at a campaign boundary, not inside one. The synthetic probe
(Amendment 7) does not help here: it runs only after the static check passes.

## Launch

First launch aborted before any trial — see `ABORTED-FIRST-LAUNCH.md`. No ledger
row was written and the budget was intact.

## Final — 20/20, gate result

| outcome | count |
|---|---|
| scored | 15 |
| rejected by the exposure floor | 4 |
| blocked proposal (counts toward N) | 1 |
| harness failures | 0 |

681 minutes. Winner **trial 15, +0.2898**.

### The gate (tier 2), computed from `nulls.json` + the ledger

| | |
|---|---|
| observed best | **+0.2898** |
| null max mean | +0.1028 |
| **p** | **0.0339** |
| reject the null at q = 0.05 | **yes** |
| deflation margin | +0.0773, clears the selection bar |
| uncorrected nominal FAR of the best | 0.0249 |

**This is the project's second tier-2 ACCEPT, and it is not a discovery.**
`nq-ml-02` also cleared this bar, at p = 0.030, and then scored **−0.18** on
validation and took a robustness veto. Tier 2 says the best of 20 beat what the
best of 20 sign-flipped versions of themselves would have scored. It does not
say the edge is real, and the two stages that killed the last one have not run.

### What is different from nq-ml-02, and why validation is worth more this time

`nq-ml-02`'s winner traded 245 times inside **14 of 100** sessions; the
committee's robustness seat killed it on concentration, and Task 69 showed the
score itself was inflated ×2.67 by denominator padding. This winner trades
**471 times across 100 of 100 sessions** and cleared Amendment 6's floor with no
margin needed. The specific failure that ended the last campaign cannot be the
one that ends this one, so a validation look actually discriminates here.

### Trial by trial

| # | result | trades |
|---|---|---|
| 0 | -0.1030 (uncharged -0.0973) | 1732 |
| 1 | **rejected** — exposed in 48 of 100 scored sessions, under the registered floor of 70% (Amendment 6) | 134 |
| 2 | **blocked proposal** | — |
| 3 | -0.1348 (uncharged -0.1219) | 748 |
| 4 | **rejected** — exposed in 55 of 100 scored sessions, under the registered floor of 70% (Amendment 6) | 3369 |
| 5 | -0.3775 (uncharged -0.3721) | 2410 |
| 6 | -0.1230 (uncharged -0.1168) | 1786 |
| 7 | -0.1635 (uncharged -0.1573) | 1462 |
| 8 | -0.2052 (uncharged -0.2017) | 2477 |
| 9 | -0.2241 (uncharged -0.2186) | 2182 |
| 10 | +0.1713 (uncharged +0.1894) | 343 |
| 11 | +0.1713 (uncharged +0.1894) | 343 |
| 12 | **rejected** — exposed in 48 of 100 scored sessions, under the registered floor of 70% (Amendment 6) | 141 |
| 13 | -0.0404 (uncharged -0.0361) | 268 |
| 14 | +0.1988 (uncharged +0.2181) | 410 |
| 15 | +0.2898 (uncharged +0.3099) | 471 |
| 16 | +0.1911 (uncharged +0.2058) | 552 |
| 17 | +0.0358 (uncharged +0.0518) | 470 |
| 18 | **rejected** — exposed in 19 of 100 scored sessions, under the registered floor of 70% (Amendment 6) | 353 |
| 19 | -0.0352 (uncharged -0.0245) | 613 |

### The search's shape

Trials 0-9: every scored candidate negative, from −0.103 to −0.378, on 748 to
3,369 trades. Trials 10-19: five positives, all in a **narrow activity band of
268-552 trades across all 100 sessions** — roughly three to five a day. The
floor closed the concentration route the last campaign exploited, and what the
search found instead was a participating strategy. That the trajectory improves
over the run is *expected under no edge* (Task 36 demonstrated exactly that on
data with nothing in it) and is not evidence of anything on its own.

### Cost measured, not argued: one wasted trial

**Trial 11 changed 26 source lines and produced a byte-identical fill log to
trial 10** — same score to four decimals, 33 minutes of real NQ data spent
re-scoring a known candidate. Third occurrence in the project after two in
`nq-ml-02`. Amendment 7's synthetic probe catches exactly this in about a second
and was off for this campaign because the amendment is unsigned. The cost of
leaving it off is now a measured number rather than an argument.

### Not run, deliberately

`--stage validate` spends the campaign's single look at the validation split.
Held for Michael. **`src/core/scoring/` and `run.py` must stay untouched until
that look is taken**, including the Amendment 6 disclosure fix recorded in
`pre-registration.md`: validation has to run under the same `code_fingerprint`
as the trials it is validating.
