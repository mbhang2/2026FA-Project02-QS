# On a quoted options surface, is there a relative mispricing large enough to capture after the spread, and does capturing it make money?

**Where this question came from, because the record should say.** Brian set it
as a challenge at the 2026-10-22 meeting: build a simulated options dataset with
many strikes and expiries and a possible arbitrage hidden in it, and find out
whether the proposer can detect it and build a detection rule that is fast and
effective. It was not distilled from a paper and no paper is cited, because none
was read for it. The dataset was built for the question rather than the question
found in the data, which is the opposite of the usual order here and worth
recording as such.

## The question

An options surface is not a collection of independent prices. Calls and puts at
one strike are tied to each other and to the forward; three strikes in a row
cannot be priced so that the middle one is dear relative to its neighbours;
a spread cannot cost less than nothing nor more than its width. These are
identities and inequalities, not forecasts — they hold whatever the market does
next, which is what makes a violation a different kind of object from an edge.

**Is there a violation on this surface that survives the spread, and does acting
on it produce a profit that the gate can see?**

Both halves matter and the second is the harder one. A relationship that fails
by less than the cost of trading the legs is not a violation anyone can take,
and a rule that reports one is reporting arithmetic rather than opportunity.

## This is a question whose answer may be "no"

The same question is being asked of two surfaces, and on one of them the honest
answer is that there is nothing there. A rule that finds an arbitrage on a
surface that contains none has found its own spread, and that is the single most
likely way to fail here — far likelier than missing a real one.

So the rule has to be capable of reporting nothing. Trading because the scan
returned something is how a detector with a loose threshold converts noise into
a position, and this harness will charge it for every leg.

## Four things about the data that will otherwise be got wrong

**Quotes are two-sided and they have sizes, and a size of zero is real.** The
side you need may have nothing behind it. An edge measured mid-to-mid is not an
edge; an edge whose exit side shows no size is not one either. Both are checked
here, because a detector that ignores size reports opportunities nobody can
take.

**There is carry, and you are not told what it is.** The riskless rate and the
dividend yield are both non-zero and neither appears on a quote. The forward is
therefore not the spot, it is not constant across expiries, and a relationship
written as though money had no time value will be wrong by an amount
proportional to how far apart the strikes are. On this ladder strikes span
several thousand points, so that error is large, it has a consistent sign, and
it looks exactly like a standing arbitrage. Expect it. Whatever you cannot
observe, you have to either eliminate or infer from the quotes themselves.

**The expiry ladder rolls.** Contracts sit on a fixed grid of sessions and the
surface lists the nearest few; as one expires another appears. `expiry_session`
identifies a contract for its whole life and is how you tell this session's
front month from last week's.

**Nothing is quoted after it expires, and a position in it is settled in cash at
intrinsic value at the close of its expiry session.** You do not have to act on
that and you cannot prevent it.

## Why the acting floor is not a problem here, and when it becomes one

The registered exposure floor asks that a strategy be in the market across most
of the scored sessions. A rule that scans every bar and acts only on what it
finds can still meet that comfortably, because a position in an option is
carried until something ends it rather than closed the same afternoon.

What will not be measurable is a rule that takes a handful of positions and then
stops. That is the rule's fault and not the floor's: if the condition it waits
for is real and standing, it recurs.

## Why it might be true, and why it might not

**Why it might.** The surface is generated, and a generated surface is only
arbitrage-free to the extent that whoever built it enforced every condition at
every strike and every expiry, in the quotes rather than in the mids. That is
more conditions than it sounds like, and quantisation to a tick alone can break
some of them.

**Why it might not.** The conditions are enforced on this surface and were
measured as holding — the baseline reports zero violations of every condition
that is valid under carry, across tens of thousands of quotes. So a scan that
reports violations everywhere is much more likely to have found a defect in its
own reasoning than a defect in the data. The prior that should be carried into
this campaign is that the surface is clean until a violation survives an
explicit, costed, size-checked accounting of what it would take to capture it.

## What a result looks like

A rule that identifies a violation, states which relationship it breaks, takes
the position that captures it, and carries it to whatever pays it — and that
reports and takes nothing on a surface where there is nothing to take.
