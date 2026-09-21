# Task 63 — the paired arms: does the idle charge stop reward hacking?

Two campaigns, identical in every respect except one, run twelve hours apart on the
same machine.

| | `nq-orderflow-01` | `nq-orderflow-02` |
|---|---|---|
| hypothesis | `orderflow-absorption.md`, digest `70bcc32a` | **the same file, same digest** |
| data | NQ train, 384 sessions | same |
| N, q, null, correction | 50, 0.05, `round_turn_mc`, `max_of_n` | same |
| model | `claude-haiku-4-5`, preflighted | same |
| commission | 0.10/side | same |
| **idle charge** | **none (`MAR = 0`)** | **0.18 index points, time-weighted** |

Everything else being equal is not a claim; it is the same file, the same digest,
the same config block, and the same preflight.

## The result

| | arm A | arm B |
|---|---|---|
| champion | trial 16, -0.0125 | trial 25, -0.4232 |
| **champion exposed sessions** | **8** of 384 | **184** of 384 |
| **champion trades** | **8** | **257** |
| **corr(exposed sessions, score)** | **-0.935** | **+0.083** |
| exposure of the top 5 | 8, 27, 26, 2, 7 | 184, 184, 186, 151, 168 |
| mean exposed sessions | 92 | 146 |
| validation change (rule c) | -0.1391 | **-0.0423** |
| gate | REJECT, p = 0.9871 | REJECT, p = 1.0000 |
| committee | VETO | VETO |
| outcomes | 50 completed | 48 completed, 1 crashed, 1 invalidated |

**The five best candidates in arm A traded in 2 to 27 sessions of 384. In arm B,
151 to 186.** The correlation between participation and score crossed from -0.935
to +0.083 -- from "trading less is the whole strategy" to no relationship.

## What was not predicted, and matters

**Arm B's winner decays less out of sample: -0.042 against -0.139.** Amendment 3
was argued entirely on the in-sample gradient; nothing in it anticipated a
generalisation effect. But it is the expected direction on reflection -- a
statistic computed from 8 trades in 8 sessions is estimated from almost nothing,
so it should move a lot when the sample changes, and one from 257 trades should
not. The charge improved out-of-sample stability by making the in-sample number
rest on more observations.

This is a second and independent argument for the charge, and it was free.

## Two numbers to read carefully

**The null bar moved from +0.0529 to -0.3476.** Every null draw pays the same idle
charge, so the whole distribution shifted down with the observed score. That is the
consistency the implementation was tested for -- a charge the observed series pays
and the null does not moves a measured p from 0.0367 to 0.1813 -- confirmed here on
real data rather than a fixture.

**p = 1.0000 in arm B: the observed best was worse than all 10,000 sign-flipped
draws.** That is a statement about the hypothesis, not about the fix. These
absorption strategies are systematically on the *wrong side*: randomising their
direction improves them. Absorption as the agent implemented it is not a weak
effect, it is an inverted one.

## What this does not show

**It does not show the charge is correctly sized.** 0.18 was derived from the cost
of capital and its empirical window checked afterwards; this pair shows the
gradient reversed, not that 0.18 is the right magnitude. A charge of 0.30 would
likely have reversed it too, and 1.00 would have over-corrected into rewarding
churn -- which the re-scoring in Amendment 3 already showed.

**It does not show absorption is absent from NQ.** Arm A never tested it; it spent
50 trials discovering inactivity scores well. Arm B did test something, and p = 1.0
says the thing it tested is inverted. Whether a better-specified absorption rule
exists is open.

**It is n = 1 per arm.** Two campaigns, not a distribution over campaigns. The
effect is large and the mechanism was derived from the arithmetic before either arm
ran, which is why it is worth reporting -- but a second pair on a different
hypothesis would be the honest confirmation.

## Accounting

Global N is **672**. Arm B's crashed trial (a 240 s gateway timeout on the propose
call) and its invalidated trial both count toward N, which is the conservative
direction: a look that produced nothing is still budgeted.

The invalidated trial is worth naming. `config/hypotheses/Test1.md` was copied into
a fingerprinted directory mid-campaign, and the artifact check invalidated the trial
in flight rather than scoring it against an artifact state that did not hold. The
guard was right and is broader than the dependency -- the campaign reads only its
own hypothesis. Recorded in `docs/setup/RUNNING-A-CAMPAIGN.md`.

## Outstanding

**Task 57's FWER measurement describes `MAR = 0` and has not been redone under the
charge.** The pre-registration's rule is that a mode labels its own claim. Nothing
currently rests on it -- both arms REJECTed, so no claim is outstanding -- and a
unit-scale check (R = 300, B = 400) moved P(p<=0.05) only 0.037 -> 0.043. It is a
**precondition for any future ACCEPT**, not a blocker on further work.
