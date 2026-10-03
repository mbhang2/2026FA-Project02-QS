# Does a break of the first 30 minutes' range continue, or fail back into it?

Written 2026-10-03, before the campaign that uses it was registered. The question is Michael's; this file states it. This revision keeps that question and turns the last version's warning about holding periods into a rule the strategy must obey.

## The question

Call the high and low of the **first 30 minutes after the regular-hours open** the opening range. Price then leaves that range, often more than once.

**When it breaks out, does it keep going, or does it come back inside?**

Two accounts are common and they predict opposite things. **Continuation:** the first half hour is where overnight information is repriced and the day's participants arrive, so a move beyond it is the day choosing a direction. **Failure:** the first half hour is thin and full of orders that had to be done at the open regardless of price, so a break of it is as likely to be that flow exhausting itself. Both are widely claimed about index futures and they cannot both be true here.

## Locating the open: Chicago time, offset taken from the calendar

The feed is full Globex in UTC and **one session spans two UTC calendar days**. Nothing in the data says where regular hours began, so the strategy must find that moment itself and **say in its docstring which convention it used** — the CME equity-index day session, 08:30–15:15 Chicago, or cash-equity hours, 08:30–15:00 Chicago. They agree at the open and differ at the close.

**The Chicago offset must be derived for the timestamp being used, never fixed.** It is -5 during daylight time and -6 otherwise, and this sample spans several changes. A previous attempt applied a fixed -6 to every session, which put the opening window one hour late on every session falling in daylight time: the 08:30–09:00 range was drawn from bars covering 09:30–10:00 Chicago on those days. Derive the offset from the date, state it in the docstring, and locate the open before any range is drawn.

## What the evidence says about the shape of the answer

The fade — selling a break above the high, buying a break below the low — was the arm implemented: 337 trades across 384 sessions, net 1133.4125 index points, 183 sessions down. The shape repeats the problem the last version warned about. Hours 14 and 15 UTC contribute 1125.47 and 1857.15 against that total; drop hour 15 and the aggregate turns negative. Thursday contributes 1062.55 and Tuesday -351.79. The largest months are 2025-10 at 694.92 and 2025-08 at 589.50, while 2025-04 alone is -399.23. The best single session is 1.72% of the total and the best 5% of sessions carry 21.8% of it.

Two findings matter more than the net. The average holding period is 18,921,814,034,363 ns, about 5.26 hours, while the docstring claimed no position reached the afternoon and that a target of 0.25x range width kept holding short. Neither was true. And the worst peak-to-trough stretch is -1302.70 against a net of 1133.4125: at its worst point the rule had lost more than it ever made.

So the exit is now part of the question. **A strategy must state the clock time by which it is flat on every session, and that time must follow from the claim it defends** — a fade of a break in the first hour is a claim about the first hour or two afterwards, not about the afternoon. If the exit cannot be stated without reference to hours the question makes no claim about, the strategy is answering a different question and must say which one. Everything below follows from that.

## What the answer must report

The question asks for a direction consistent across the ordinary run of sessions. So alongside its net the strategy reports the hour-of-day split, the weekday split, and the month-by-month result, and says what it expects of a break on each weekday. A net carried by Thursday and undone on Tuesday, or carried by two months and undone in 2025-04, is not the answer being asked for. Sessions where the sign flips belong to the answer, not to the noise. The drawdown above is the same demand in another form: the answer must survive being traded.

## What is deliberately not fixed

What counts as **a break** — any trade beyond the range, a bar closing beyond it, a move measured against the range's own width or the session's volatility so far, or one that holds for some time. And what counts as the answer: how far price must travel for continuation, how long to wait before calling it a failure, and whether the two directions behave alike. Each choice is a different claim and its own proposal. A fixed number of points is a different question on a quiet morning than on a busy one.

One condition is worth adding, and it comes from 2605.11423v3: the size of the first 30 minutes' return against its recent norms, the size of the overnight gap, and the first bar's volume against its recent baseline, all known when the opening range closes. Together they say whether that range was built in thin forced flow or in an outsized move — exactly the fork between the two accounts. The strategy states what it expects of a break on each kind of session and reports them separately. This is a description, not a switch between the accounts; the paper cautions against narrowing to the extreme sessions, since the days it identifies (about 4.4% of its sample, 40 days of 947) drifted +40.74 points to the close in one year and -42.48 in the next.

## Thirty minutes, not the conventional sixty

The usual initial balance is the first hour. Halving it narrows the range and makes breaks more frequent: more occasions to be right, and more of them noise. If thirty minutes is too short for the range to mean anything, that is a real finding about the question and better said plainly.

## Papers considered and not used

2608.00885v1 builds its band around a latent efficient price estimated from the mid and the spread; this feed has no quotes and no book, so that state cannot be formed here. 2607.20093v1's one usable point — that a rule resting on the clock alone is weak, and that its calendar family did not survive its own tests — is already carried here twice: breaks scaled to the range's own width, and the weekday report the evidence now forces. Neither offers anything further.

## What this is not asking for

- Not a search across many instruments. One instrument, this data.
- Not a book- or quote-based measure. The feed is trades with an aggressor side.
- Not an overnight or Globex range. The regular-hours open specifically.
- Not a regime model switching between the two accounts. If the answer is "it depends", that is a later and harder question.
- Not a fast-execution idea. It should survive fills worse than the prices that generated the signal.

## Why this one

It states in a sentence, it has a real direction to be wrong in, it concerns a time of day defined in advance rather than found afterwards, and it is not one of the families already falsified on this data — those are listed under *Falsified hypothesis families* in `docs/datasets/nq-es-qqq.md`. The revision adds one rule, a stated exit time, and names the reports the answer must carry; the rest is the parent, trimmed.
