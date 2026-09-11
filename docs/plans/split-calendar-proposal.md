# Split Calendar — Proposal for Decision (v2)

Supersedes the earlier draft, which was written against 521 sessions before the data
extension. Numbers here are from `c5eafcd`.

**This is a human pre-registration.** The boundaries are chosen before any outcome is
observed, and their entire value comes from having been chosen first. Commit this
document before deciding, so the sequence is dated and provable — a pre-registration
that cannot be dated is not one.

## What we have

| | |
|---|---|
| NQ assemblable sessions | **564**, 2024-07-04 → 2026-09-09 |
| NQ ∩ same-day QQQ options | **547** |
| ES assemblable / ∩ options | 307 / 297 |
| `near_roll` sessions | 39, all within 4 days of a quarterly roll |
| Genuine data losses | 2 (2025-04-18, 2024-07-03) |

The 17 NQ sessions outside the options intersection are all CME-open /
US-equity-options-closed holidays. The category that mattered — sessions past the end of
OPRA coverage — is now empty and a test asserts it stays empty.

## Boundaries are dates, not session counts

The calendar declares **dates**. Each dataset derives its own session list from them.

That keeps the NQ trades dataset at its full 564 sessions while a future GEX-using
dataset gets 547 over the *same time periods* — aligned splits, no dataset's data
availability constraining another's. Global N spans datasets regardless, so differing
per-dataset counts cost nothing.

## The three constraints

**Validation must be large enough to search on.** The loop scores hundreds of candidates
against it, and it *will* be overfit by selection. That is expected; it is why the
holdout exists.

**The holdout must be large enough to mean something.** The calibration floor is
`MIN_SESSIONS = 30`, and the tabulated null covers n = 10…200, so a 73-session holdout
is comfortably inside the range where the threshold is a measurement rather than an
extrapolation: `threshold_for(0.001, 73)` returns a Sortino of 0.0758 and `far_for`
inverts it to 0.001 exactly. The binding constraint is power, not validity.

(The draft cited "`threshold_for(0.001, 50)` at 0.07% false acceptance". That does not
reproduce and appears to conflate two numbers: `threshold_for` returns a *score*, 0.1964
at n=50, and the false-acceptance rate it delivers is the 0.1% that was asked for. The
figure Task 6 actually measured is 0.11% realized false acceptance for the empirical
rule against 6.1% for the parametric one. The argument is unaffected; the number was
wrong.)

**The holdout must postdate the backbone model's cutoff** (end of May 2026), or a
memorization result is indistinguishable from a real one.

The extension resolved the tension that made this hard: constraints 2 and 3 no longer
compete for the same few dozen sessions.

## Recommended split

| Split | Dates | ≈ NQ sessions |
|---|---|---|
| Split | Dates | NQ sessions | GEX-eligible |
|---|---|---|---|
| **Train** | 2024-07-05 → 2025-12-31 | **384** | 375 |
| *embargo* | 10 sessions | −10 | |
| **Validation** | 2026-01-16 → 2026-05-15 | **86** | 83 |
| *embargo* | 10 sessions | −10 | |
| **Holdout** | 2026-06-01 → 2026-09-09 | **73** | 70 |

71 / 16 / 13 excluding embargo. Counts computed from the published session list at
`c5eafcd`, not estimated: the draft's ~361 / ~83 / ~67 were low, most materially on the
holdout, which has 73 sessions rather than 67. The guessed validation start of
2026-01-16 is exactly right — it is the tenth session after 2025-12-31.

The embargo works out to **exactly 10** sessions at each boundary, with no slack: the
last validation session is 2026-05-15 and the tenth session after it is 2026-06-01, the
proposed holdout start. Moving either date by one session breaks the embargo the
`Calendar` constructor enforces, so these two dates are load-bearing rather than
round-numbered.

Twenty-one of the 564 sessions are unused: the twenty embargo sessions, plus 2024-07-04,
which falls before the train start. Starting train on 2024-07-04 instead would recover
it for the NQ trades dataset; 2024-07-05 is where the options intersection begins, which
is presumably why the draft chose it.

The holdout sits **entirely after May 2026**, satisfying the cutoff constraint without a
separate carve-out, and the calendar-quarter boundaries make it easy to state and hard
to misremember.

*Alternative considered and not recommended:* moving the train/validation boundary back
to 2025-10-31 gives **128** validation sessions against **342** train. Better search signal,
weaker fitting for any ML-shaped strategy. Take it only if the team expects heavily
fitted candidates rather than rule-based ones.

## The embargo, and how it is enforced

Ten sessions at each boundary, so training and validation observations do not share the
data they touch.

Enforcement is one assertion at config load:

```
warmup_sessions <= embargo_sessions
```

A strategy cannot see more history than the run profile feeds it, so the embargo holds
by construction provided warmup does not exceed it. Task 13 implemented this check. It
is **not** inert — it is live for any dataset with a calendar, and already runs against
the synthetic dataset's embargo of 2 on every `run.py` invocation. It stands down only
for a dataset whose calendar is not written, which today means NQ; committing this file
is what brings NQ under it. The harness and agent profiles use `warmup_sessions: 1`, so
an embargo of 10 leaves nine sessions of headroom.

## Holdout procedure

- `run.py` refuses the holdout unless explicitly unlocked. This already works.
- **Opened once**, at the end, by a deliberate act recorded in the ledger.
- **Known caveat:** vendor `condition.json` coverage stops at 2026-07-03, so **48 of
  the 73 holdout sessions** — two thirds of it — have no vendor data-condition record.
  (47 is the count of OPRA *files* in the uncovered range, not of holdout sessions; the
  two are close but not the same number.) Condition there is *unknown*, not good. Before opening the holdout, the
  robustness auditor should scan that window for anomalies rather than assume it is
  clean. Record this in the writeup; it is a limitation, not a defect.

## What this does not resolve

**No split fixes the power problem.** 73 holdout sessions cannot carry the primary
statistical argument wherever the boundaries go. The division of labour should be stated
explicitly rather than discovered:

- **CPCV on train + validation carries the inference** — purged and embargoed, with the
  ledger's FAR column and global N supplying the multiple-testing correction.
- **The holdout is confirmatory.** It answers "did the selection procedure fool itself",
  not "is this strategy good".

ES's 297 options-eligible sessions remain available as a transfer test, subject to the
pre-registered rule that **a transfer test which can veto a candidate is selection and
counts toward N**, while one that is merely reported does not. Which it is must be
decided in advance.

## Questions for Peterson

1. Is 13% a defensible holdout fraction given hundreds of automated trials against
   validation? The ML literature's conventions are not obviously the right reference for
   search this aggressive.
2. Does QuantStrat have a house standard for holdout budgeting under repeated automated
   search — or is the honest answer that this pattern is new enough not to have one?
3. Is the confirmatory framing acceptable — CPCV carrying the inference, the holdout
   checking the procedure rather than the strategy?
4. Open the holdout once at the end, or a small number of pre-registered openings with an
   alpha budget spent across them? The second gives earlier feedback at a stated cost.

## The file to commit

`config/calendars/nq-trades.yaml`, path derived from `Dataset.name`:

```yaml
dataset: nq-trades
pre_registered: 2026-09-11    # must be the date this is actually committed
embargo_sessions: 10

splits:
  train:
    start: 2024-07-05
    end:   2025-12-31
    reason: >
      Earliest available session through calendar year end. Bulk-served to setup().
  validation:
    start: <first session >= 10 sessions after train end>
    end:   2026-05-15
    reason: >
      Ends before the backbone model cutoff so the holdout can lie entirely after it.
      The loop scores here repeatedly and this split is expected to be overfit by
      selection.
  holdout:
    start: 2026-06-01
    end:   2026-09-09
    reason: >
      Entirely post-cutoff. Opened once, by deliberate unlock, recorded in the ledger.
      Vendor condition records are absent from 2026-07-06 onward; scan for anomalies
      before opening.
```

Commit this proposal first, then the YAML, then `run.py` finally has a split to serve.
---

## Verification against `c5eafcd`

Checked mechanically before this document was committed, so the pre-registration is
dated *and* arithmetically sound. What held:

- 564 NQ sessions, 2024-07-04 → 2026-09-09; 547 with same-day options; ES 307 / 297.
- 39 `near_roll` sessions, and **every one is within four calendar days of a roll** —
  the claim, not just the count.
- Two genuine losses, 2025-04-18 and 2024-07-03.
- 71 / 16 / 13 as a share of the sessions actually used.
- The guessed validation start, 2026-01-16.

What was corrected: the three session counts (384 / 86 / 73, not ~361 / ~83 / ~67), the
alternative split's counts (342 / 128), the condition.json caveat (48 holdout sessions,
not 47), and the Task 6 calibration figure, which did not reproduce.

Two things worth knowing that the draft does not mention:

**Every split contains at least one complete roll** — train six, validation one
(2026-03-16), holdout one (2026-06-15) — and no boundary date falls inside a roll
window; the closest is the holdout start at 14 days. So no split is roll-free while
another carries all of the roll convention's noise, which would otherwise be a
difference between splits that has nothing to do with strategy quality. `near_roll`
sessions land 30 / 5 / 4.

**The calendar is not written.** This document is a proposal with four open questions;
`config/calendars/nq-trades.yaml` does not exist and was deliberately not created. The
split is a human pre-registration and the sequence — decide, then commit, then run — is
the entire source of its value.
