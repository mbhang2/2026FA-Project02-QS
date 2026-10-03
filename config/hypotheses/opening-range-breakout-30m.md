# Does a break of the first 30 minutes' range continue, or fail back into it?

Written 2026-10-03, before the campaign that uses it was registered. The question
is Michael's; this file states it.

## The question

The regular-hours session opens and, for a while, the instrument trades out a
range. Call the high and low of the **first 30 minutes after the regular-hours
open** the opening range. Price then leaves that range in one direction or the
other, often several times.

**When price breaks out of that opening range, does it keep going, or does it
come back inside?**

Two accounts are common and they predict opposite things:

- **Continuation.** The first half hour is where overnight information is
  repriced and where the day's participants show up. A move beyond that range is
  the day choosing a direction, and the rest of the session extends it.
- **Failure.** The first half hour is thin, noisy, and full of orders that had to
  be done at the open regardless of price. A break of it is as likely to be the
  exhaustion of that flow as the start of anything, and price returns into the
  range.

Both are widely claimed about index futures. They cannot both describe this
instrument at this time of day.

## What "the regular-hours open" has to mean here, and why it is not obvious

The data is full Globex in UTC, and **one session spans two UTC calendar days**.
There is no field in the feed that says "the regular hours started here". The
strategy has to locate that moment itself, from the session calendar and the
clock, and it must **say in its docstring which convention it used**:

- the CME equity-index **day session**, 08:30-15:15 Chicago time, or
- the **cash-equity hours**, 08:30-15:00 Chicago time.

They differ by fifteen minutes at the close and not at all at the open, so for
this question the open is the same either way -- but the convention still has to
be written down, because the choice changes what "the rest of the session" means
and two strategies that disagree about it are answering different questions.

**Chicago time, not UTC.** The offset moves twice a year and the sample spans
several of those changes. A strategy that hard-codes a UTC hour is asking about a
different clock-time window in summer than in winter.

## What "a break" should mean is deliberately not fixed

A break could be any trade beyond the range, a bar closing beyond it, a move of
some size beyond it measured against the range's own width or the session's
volatility so far, or a break that holds for some time. These are different
claims and each is its own proposal. Choosing that measure well is part of the
work, and a fixed number of points is a different question on a quiet morning
than on a busy one.

The same is true of what happens next: how far price has to go for the move to
count as continuation, how long to wait before calling it a failure, and whether
the two directions behave the same way.

## Thirty minutes, not sixty

The conventional initial balance is the **first hour**. This question asks about
the first **half** hour, which is a deliberate departure and worth stating: a
shorter window makes the range narrower and the breaks more frequent, which cuts
both ways -- more occasions to be right, and more of them noise. If the honest
answer turns out to be that thirty minutes is too short for the range to mean
anything, that is a real finding about the question and better said than
dressed up.

## What would make the answer interesting

That the direction is consistent. A story that only works on the largest few
mornings in the sample is a story about those mornings. What would be worth
knowing is whether the effect is there across the ordinary run of sessions, in
one direction, and whether it survives being traded rather than merely observed
-- entry, exit and holding are part of the claim, not details to be added later.

Because this question names a specific part of the day, the strategy should say
what it expects outside that part. A claim about the half hour after the open is
not a claim about the whole session, and the result should not quietly become
one.

## What this is not asking for

- Not a search across many instruments. One instrument, this data.
- Not a book- or quote-based measure. The feed is trades with an aggressor side;
  there is no resting-liquidity data, no depth, no bid-ask.
- Not an overnight or Globex-session range. The question is about the regular
  hours open specifically.
- Not a regime model that switches between the two accounts. If the answer is
  "it depends", that is a later and harder question; answer the simple version
  first.
- Not a fast-execution idea. The claim should survive being filled at prices that
  are worse than the ones that generated the signal.

## Why this one

It is simple enough to state in a sentence, it has a real direction to be wrong
in, it concerns a time of day that is well defined rather than discovered after
the fact, and it is not one of the hypothesis families this project has already
falsified on this data -- those are listed under *Falsified hypothesis families*
in `docs/datasets/nq-es-qqq.md`, and re-exploring one spends effort on a known
answer.
