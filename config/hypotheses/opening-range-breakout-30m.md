# Does a break of the first 30 minutes' range continue, or fail back into it?

Written 2026-10-03, before the campaign that uses it was registered. The question
is Michael's; this file states it.

## The question

Call the high and low of the **first 30 minutes after the regular-hours open** the
opening range. Price then leaves that range, often more than once.

**When it breaks out, does it keep going, or does it come back inside?**

Two accounts are common and they predict opposite things. **Continuation:** the
first half hour is where overnight information is repriced and the day's
participants arrive, so a move beyond it is the day choosing a direction.
**Failure:** the first half hour is thin and full of orders that had to be done at
the open regardless of price, so a break of it is as likely to be that flow
exhausting itself. Both are widely claimed about index futures and they cannot
both be true here.

## Locating the open is the part most likely to go wrong

The feed is full Globex in UTC and **one session spans two UTC calendar days**.
Nothing in the data says where regular hours began, so the strategy must find that
moment itself and **say in its docstring which convention it used** — the CME
equity-index day session, 08:30–15:15 Chicago, or cash-equity hours, 08:30–15:00
Chicago. They agree at the open and differ at the close, so the choice changes
what "the rest of the session" means.

**Chicago time, not a fixed UTC hour.** The offset moves twice a year and this
sample spans several of those changes.

## What is deliberately not fixed

What counts as **a break** — any trade beyond the range, a bar closing beyond it,
a move measured against the range's own width or the session's volatility so far,
or one that holds for some time. And what counts as the answer: how far price must
travel for continuation, how long to wait before calling it a failure, and whether
the two directions behave alike. Each choice is a different claim and its own
proposal. A fixed number of points is a different question on a quiet morning than
on a busy one.

## Thirty minutes, not the conventional sixty

The usual initial balance is the first hour. Halving it narrows the range and
makes breaks more frequent, which cuts both ways: more occasions to be right, and
more of them noise. If thirty minutes turns out to be too short for the range to
mean anything, that is a real finding about the question and better said plainly.

## What would make the answer interesting

That the direction is consistent across the ordinary run of sessions, in one
direction, and that it survives being traded rather than merely observed — entry,
exit and holding are part of the claim. Because the question names a specific part
of the day, the strategy should also say what it expects outside that part; a
claim about the half hour after the open must not quietly become a claim about the
whole session.

## What this is not asking for

- Not a search across many instruments. One instrument, this data.
- Not a book- or quote-based measure. The feed is trades with an aggressor side:
  no depth, no bid-ask, no resting liquidity.
- Not an overnight or Globex range. The regular-hours open specifically.
- Not a regime model switching between the two accounts. If the answer is "it
  depends", that is a later and harder question.
- Not a fast-execution idea. It should survive fills worse than the prices that
  generated the signal.

## Why this one

It states in a sentence, it has a real direction to be wrong in, it concerns a
time of day that is defined in advance rather than found afterwards, and it is not
one of the families already falsified on this data — those are listed under
*Falsified hypothesis families* in `docs/datasets/nq-es-qqq.md`.
