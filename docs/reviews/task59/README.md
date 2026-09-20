# Task 59 — the power of `round_turn_mc`, and what the cheap null cost

**Written before anything ran.** Nothing below is adjusted in response to a
result.

## Why

Task 57 measured the family-wise error rate of `round_turn_mc` + `max_of_n` at
**0.0000 — zero accepts in 800 known-null campaigns**, at both N = 10 and N = 50,
against 0.060 for the conditional null. Perfect error control.

**A gate that controls FWER perfectly and accepts nothing is indistinguishable,
from every number currently recorded, from a gate that works.** That sentence is
Task 33's, written when it found a rule with 0% power *by accident* while
implementing it for an unrelated reason. This measurement is the one that tells
the two apart, and until it exists `nq-mvp-01`'s REJECT means nothing: it may be
a gate rejecting a bad campaign, or a gate that rejects everything.

## The mechanism this is testing, stated so it can be wrong

Each candidate's sign-flip null spans **its own** ±gross range — within-candidate
variability. But `max_of_n` compares the best *observed* against the best
*simulated*, and the observed candidates in a campaign are variants of one
strategy family on one dataset, so they cluster. **The max of ten tightly
clustered observations loses to the max of ten wide nulls.**

If that is right, power is not merely reduced — it is reduced in a way that
depends on how much the observed candidates spread relative to each candidate's
own sign-flip spread, and a planted signal has to be large enough to pull the
observed maximum clear of nulls that are wide by construction.

Ruled out already: low turnover. Task 57's candidates had a **median of 470 round
turns and a minimum of 89**, so the nulls are not wide because there was nothing
to flip.

## The pre-registration

**Identical to Task 33's power design, so the two curves are comparable.**

| | |
|---|---|
| strengths | **0.0, 0.15, 0.3, 0.5, 0.8** |
| campaigns per strength | **C = 60** |
| trials per campaign | **N = 10** |
| null source | `round_turn_mc`, **B = 10,000** |
| correction | `max_of_n`, q = 0.05 |
| dataset | synthetic, 175 × 390, `vol_per_tick` 0.15, `DATASET_SEED = 4_242` |
| candidates | `NullGenerator`, seeded per strength as Task 33 seeded it |

**One construction difference, and it is not a choice.** Task 33 built its nulls
on signal-free data, because a tabulated null containing the signal would have
nothing left to detect. `round_turn_mc` has no such option: the null *is* the
candidate's own realised round turns, which were made on signal-bearing data.
Sign-flipping still destroys direction, so it remains a legitimate null — but it
is constructed differently, and the comparison is between two configurations'
outcomes rather than between two nulls built the same way.

## What it can resolve, before it runs

At C = 60 the binomial standard error at a true power of 0.8 is
`sqrt(0.8 × 0.2 / 60) = 0.052`, so a 95% half-width of **0.10**. It locates the
80%-power crossing to within roughly one grid step and **cannot distinguish 0.75
from 0.85**. Same resolution as Task 33, deliberately.

## What each outcome would mean

- **A curve close to Task 33's** (80% near strength 0.24) — the cheap null costs
  little, the FWER of 0.0000 is honest conservatism, and `round_turn_mc` stays the
  default.
- **A curve shifted right** — it works but needs a bigger effect. The shift is the
  price of the 145-second null, and it is quotable: "detects at strength s where
  the conditional null detects at 0.24".
- **Power near zero at every strength** — `round_turn_mc` is dead as a gate. The
  default returns to `conditional`, campaigns cost ~12 hours of nulls again, and
  the iterate-fast benefit was illusory. `nq-mvp-01` would then need re-gating from
  its stored fill logs, which is possible and costs no re-running.

**The prior, recorded so it can be wrong:** shifted right, crossing 80% somewhere
between 0.5 and 0.8. At strength 0.5 Task 33 measured a median best Sortino of
**6.76**, which should clear almost any null; at 0.15 it measured 0.095, which
should not.

## Accounting

`RunTicket.for_null_calibration`, so none of these 3,000 runs is a trial and none
touches N.
