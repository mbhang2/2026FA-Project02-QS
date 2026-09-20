# Task 62 — `nq-orderflow-01`, and what it found instead

The artefacts are `runs/campaigns/nq-orderflow-01/`. The runner is
`scripts/nq_campaign.py`; the protocol is `docs/design/pre-registration.md`. The
hypothesis is `config/hypotheses/orderflow-absorption.md`, digest `70bcc32a...`,
registered before the run and re-verified at every stage.

## The configuration

| | |
|---|---|
| data | NQ trades, **train** split, 384 sessions |
| N | 50, `q` = 0.05 |
| null source | `round_turn_mc`, B = 10,000 |
| correction | `max_of_n` |
| commission | 0.10 index points per side — **actually charged this time** (Task 60) |
| model | `claude-haiku-4-5`, preflight verified |
| cost | 251 min for the trials, 147 s for the nulls, 62 s for validation |
| outcomes | **50 of 50 completed.** No crashes, one supervisor attempt |

## The result

```
gate        REJECT      p 0.9871   observed -0.0125   5% bar +0.0909
validation  train -0.0125 -> validation -0.1516   change -0.1391
            rule (a) Sortino > 0 on validation: FALSE
tier 3      CANNOT_TELL, escalate (the platform isolation caveat)
committee   VETO -- statistics, economics, robustness.  leakage PASS.
```

Every view rejects, and the observed best is **negative**, so the Amendment 1
floor would have refused it even had the null been cleared.

## What it actually found

**Not an answer about absorption. An answer about the metric.**

The agent found the degenerate optimum at trial 16 — **8 trades in 384 sessions** —
and nothing in the remaining 34 trials beat it. The pattern is unambiguous:

| | |
|---|---|
| top 5 trials by score | are **exactly** the 5 least active |
| median Sortino, >= 100 exposed sessions (11 trials) | **-0.2212** |
| median Sortino, < 50 exposed sessions (7 trials) | **-0.0357** |
| corr(exposed sessions, score) | **-0.949** |

This is reward hacking, named by Brian Peterson on 2026-09-20: *"if trading is
most likely to result in a loss (no edge), then the agent will learn to not
trade."* The arithmetic is in Amendment 3 — `sortino` goes as `1/sqrt(n)` for a
fixed set of trades, so padding the return vector with flat sessions drags a
negative score toward zero, and `MAR = 0` makes that padding free.

**The gate was never fooled.** A p-value is a rank statistic, so the common
`1/sqrt(n)` factor cancels between a candidate and its own null: measured p is
0.8846 at every padding level while the observed score moves from -0.2398 to
-0.0774. Both this campaign and `nq-mvp-01` rejected correctly. What the padding
corrupts is the agent's hill-climb, and — because `max_of_n` picks the best by raw
score — which candidate a campaign selects when participation varies.

## Two findings about the apparatus

**The economics auditor reached the diagnosis independently.** From its evidence
slice, with no prompt about reward hacking and no knowledge of Brian's remark:

> "Trade count is degenerate: 8 trades across 384 sessions (0.021 trades per
> session) on train... This is not a signal; this is inactivity. **A strategy that
> trades almost never will score well when activity is costly, and that is what
> has happened here.**"

It also did the cost arithmetic correctly in index points — commission 178% of
gross on train — which is what the Amendment-2 units fix was for, after the first
live run read points as dollars.

**The winner was scored on Sharpe, not Sortino**, and statistics caught it: 2
downside sessions against the floor of 5, so the fallback fired. That is the same
degeneracy Amendment 3 predicts the idle charge removes, since a charged idle
session sits below MAR and the downside denominator stops being estimated from
almost nothing.

## Firsts

- First campaign through **`--stage validate`**: one look at the validation split,
  `--purpose check`, not counted toward N, refused on a second run.
- First whose **committee ran inside the report stage** rather than by hand.
  Validation evidence reached statistics, economics and robustness; leakage saw
  only source, as designed.
- First to use **`bar.delta`** (Task 61). All 50 trials read signed volume off
  1-minute bars and **none** subscribed to ticks; the engine stepped 0.373-0.375%
  of 141.6 M ticks throughout. A tick subscription would have been about 312x the
  work and put N = 50 out of reach.

## Accounting

Global N is **622**. All 50 rows are `checkable` — the campaign ran on committed
code, so none is `unpinned`. `ledger_check` reports no `mismatch` and no `stale`.

## What this campaign is for, now

**It is retained deliberately as the unconstrained arm.** It was scored under
`MAR = 0` with no idle charge and is not comparable to anything after Amendment 3
lands. The follow-up re-runs this hypothesis, this data, this N and this model
with the charge on, so the two differ in exactly one thing.

## What it does not say

It does not say absorption is absent from NQ. The agent never tested absorption
properly — it spent 50 trials discovering that inactivity scores well. The most
that can be said about the hypothesis is a cost observation from the first ten
trials, before the collapse: gross P&L per trade ranged -0.3025 to +0.1481 against
a flat commission of 0.20, and **none of the ten cleared it**. That is a real
observation against the hypothesis's own stated bar — *"an effect that only pays
before commission is not an effect"* — but it is ten trials, not a result.
