# Does a break of the first 30 minutes' range continue, or fail back into it?

Written 2026-10-03, before the campaign that uses it was registered. The question is Michael's; this file states it. This revision keeps that question and says plainly what the last version left to the implementer.

## The question

Call the high and low of the **first 30 minutes after the regular-hours open** the opening range. Price then leaves that range, often more than once.

**When it breaks out, does it keep going, or does it come back inside?**

Two accounts are common and they predict opposite things. **Continuation:** the first half hour is where overnight information is repriced and the day's participants arrive, so a move beyond it is the day choosing a direction. **Failure:** the first half hour is thin and full of orders that had to be done at the open regardless of price, so a break of it is as likely to be that flow exhausting itself. Both are widely claimed about index futures and they cannot both be true here.

## Locating the open: Chicago time, offset taken from the calendar

The feed is full Globex in UTC and **one session spans two UTC calendar days**. Nothing in the data says where regular hours began, so the strategy must find that moment itself and **say in its docstring which convention it used** — the CME equity-index day session, 08:30–15:15 Chicago, or cash-equity hours, 08:30–15:00 Chicago. They agree at the open and differ at the close, so the choice changes what "the rest of the session" means.

**The Chicago offset must be derived for the timestamp being used, never fixed.** It is -5 during daylight time and -6 otherwise, and this sample spans several changes. A previous attempt applied a fixed -6 to every session, which put the opening window one hour late on every session falling in daylight time: the 08:30–09:00 range was drawn from bars covering 09:30–10:00 Chicago on those days. Measured that way, part of the sample is about a mid-morning range and says nothing about the open. Derive the offset from the date, state it in the docstring, and locate the open before any range is drawn.

## What the evidence says about the shape of the answer

The fade — selling a break above the high, buying a break below the low — was the arm implemented, and it did most of its work in one hour: hour 15 UTC alone accounts for 3632.2 index points against a total of 1048.625, while other hours are mostly negative or near zero. It did most of its work on two weekdays: Friday at 1284.38 and Thursday at 1035.35, against Wednesday at -1266.25. It lost 885.93 in November 2025 alone. The best single session is 2.3% of the total and the best 5% of sessions carry 20.6% of it.

Two things follow. The mechanism named here lives near the open, so some concentration in the hours just after it is expected and is not by itself a reason to change the question. But a fade of a break is a claim about what price does *after* the break, and the average holding period here runs hours rather than the minutes this question talks about. **A strategy answering this question must say where it expects to be out of the market, and must not carry a position into hours it makes no claim about.** If profit is accruing at hour 15 UTC because a position opened around hour 13 is still live, that is a different claim than the one written here, and the docstring should say which one it is defending.

The week-and-month instability is the harder objection. The question asks for a direction consistent across the ordinary run of sessions. A net that is positive on Friday and Thursday and negative on Wednesday is not that, and neither is one undone by a single month. **The strategy should say what it expects of a break on any given weekday and report the month-by-month result as part of the claim**, not as a footnote. Sessions where the sign flips belong to the answer, not to the noise.

## What is deliberately not fixed

What counts as **a break** — any trade beyond the range, a bar closing beyond it, a move measured against the range's own width or the session's volatility so far, or one that holds for some time. And what counts as the answer: how far price must travel for continuation, how long to wait before calling it a failure, and whether the two directions behave alike. Each choice is a different claim and its own proposal. A fixed number of points is a different question on a quiet morning than on a busy one.

One condition is worth adding, and it comes from 2605.11423v3: the size of the first 30 minutes' return against its recent norms, the size of the overnight gap, and the first bar's volume against its recent baseline are all known at the moment the opening range closes. Together they say whether that range was built in thin forced flow or in an outsized move — which is exactly the fork between the two accounts above. The strategy should state what it expects of a break on each kind of session and report them separately. This is a description, not a switch between the two accounts, which the question rules out; and the paper's own result cautions against narrowing to the extreme sessions, since the days it identifies (about 4.4% of its sample, 40 days of 947) drifted +40.74 points to the close in one year and -42.48 in the next.

## Thirty minutes, not the conventional sixty

The usual initial balance is the first hour. Halving it narrows the range and makes breaks more frequent, which cuts both ways: more occasions to be right, and more of them noise. If thirty minutes turns out to be too short for the range to mean anything, that is a real finding about the question and better said plainly.

## What would make the answer interesting

That the direction is consistent across the ordinary run of sessions, in one direction, and that it survives being traded rather than merely observed — entry, exit and holding are part of the claim. Because the question names a specific part of the day, the strategy should also say what it expects outside that part. The evidence above is what "consistent across the ordinary run" has to mean in practice: not concentrated in one hour of the day, not carried by two weekdays, not undone by one month.

## Papers considered and not used

2608.00885v1 builds its band around a latent efficient price estimated from the mid and the spread; this feed has no quotes and no book, so the state it trades on cannot be formed here. 2607.20093v1's findings concern the five retail signal families on daily bars, and the one thing it offers this question — that a rule resting on the clock alone is weak — is already carried by the insistence that a break be scaled to the range's own width.

## What this is not asking for

- Not a search across many instruments. One instrument, this data.
- Not a book- or quote-based measure. The feed is trades with an aggressor side: no depth, no bid-ask, no resting liquidity.
- Not an overnight or Globex range. The regular-hours open specifically.
- Not a regime model switching between the two accounts. If the answer is "it depends", that is a later and harder question.
- Not a fast-execution idea. It should survive fills worse than the prices that generated the signal.

## Why this one

It states in a sentence, it has a real direction to be wrong in, it concerns a time of day that is defined in advance rather than found afterwards, and it is not one of the families already falsified on this data — those are listed under *Falsified hypothesis families* in `docs/datasets/nq-es-qqq.md`. The revision is longer than its parent mainly because it states the timezone requirement as a hard rule rather than a caveat, and because it names what a consistent answer must survive: the hour, the weekday and the month splits the evidence shows.
