# At a volume level carried from earlier sessions, does flow that fails to move price precede a move away from it?

Written 2026-10-17, before the campaign that uses it is registered. This is the fifth time this project has pointed at order flow and the first to ask for a held move rather than a traded one. The four before it are the reason the question is shaped the way it is, and they are described below rather than summarised away.

**Where this question came from, because the record should say.** Michael specified the setup — the levels, the absorption signature and the confirming bar — as a strategy he wanted tested, and asked for it directly. It was not written by the refining agent from a previous campaign's evidence, and it was not distilled from a paper. No paper is cited here because none was read for it. That provenance is worth recording: a question a person brought is a different kind of object from one the loop produced, and the only honest way to compare the two later is to know which this was.

## The question

Build a volume profile from trades: the price with the most volume is the point of control, the band holding the bulk of the volume has a high and a low edge, and within the distribution there are prices where a lot traded and prices where little did. Carry those levels forward from the two prior sessions and from the current session as it develops. A level is **naked** if price has not returned to it since it formed.

Price arrives at one of these levels. Sometimes a great deal of aggressive one-way flow arrives with it and price does not go anywhere: the bar makes a long tail, and it closes back past the previous bar's extreme instead of extending. Call that **absorption** — the flow is being taken by something that is not moving.

**Does absorption at a carried level precede a move away from that level, large enough to reach the next level in the profile?**

Two accounts predict opposite things. **It precedes a move away:** the aggressive side is being filled by resting size that has a reason to be there, and when the aggressors are finished the price goes the other way with nothing in its path until the next node. **It precedes nothing:** absorption is what a busy level looks like from the tape whether or not anyone is defending it, and the apparent pattern is the level being a level.

**Which one happens is yours to find out, and so is which side to take.**

## The second question, which is about the entry and not the market

A common practice is to wait after absorption for a confirming bar before acting — a bar that moves price efficiently relative to the flow behind it, and whose body is large relative to recent range. Call that **initiation**.

Initiation might improve the outcome by discarding absorption that led nowhere. It might also merely delay the entry into a move that had already begun, giving away the first part of it for a worse price.

**This campaign asks which.** A rule that acts on absorption alone and a rule that waits for initiation are both answers, and so is one that waits only under a stated condition. Waiting is **optional and it is the point** — the campaign is trying to find out whether it helps, so a rule that never waits is as useful as one that always does.

**Tag the entry with which it was.** Every entry fill must carry a tag saying whether it was taken on absorption alone or after a confirming bar; the tag reaches the record and is readable per fill alongside price, size and time. This is the only reason the question is answerable at all: rules differ from each other in their levels, their definition of absorption and their target, so comparing the ones that waited against the ones that did not would be comparing everything else about them at the same time. The tag makes the comparison readable inside a single rule, where nothing else differs.

A rule may also take both entries on the same setup, tagged apart, and let the record say which paid — that is the cleanest form of the comparison and it is allowed. It is not required, because taking both doubles what the rule does on every setup and that is its own decision with its own cost.

A workable definition of the efficiency a confirming bar should show, offered because leaving it undefined makes the two rules incomparable: the points a bar travels per unit of signed flow behind it. Track that quantity separately for bars whose flow was net one way and bars whose flow was net the other, over a short rolling history, and treat a bar as initiating when its efficiency stands well above that recent distribution and its body is a large fraction of recent average range. **A bar whose signed flow is near nothing makes this quantity meaningless and must be handled explicitly rather than divided by.** How far above, and over how long a history, is yours to choose.

## What the four previous order-flow attempts did

All four ended below where they began. Two asked about absorption and two about whether flow continuation persists; the question is not new here and neither are the features.

What is new is knowing why they failed, which is not the same as the idea being wrong. Three of the four lost money before costs were counted at all. The fourth made money before costs: about 0.09 index points for each round turn it did, against 0.2 points of commission for the round turn and the spread on top of that. It did over seven hundred round turns. **No signal that size survives this cost model at that frequency, and none of the four was ever going to.**

That is the constraint this question is built around. A rule here must earn multiple points per round turn, which means it must hold for a move between levels rather than trade the flow itself.

## Why the acting floor is lower for this question

Amendment 6 freezes an exposure floor per campaign and says the hypothesis is what declares a phenomenon rare. This one declares it.

Price does not arrive at a carried level with absorption on it every session, and a rule forced to act every session would be answering a different question — it would have to weaken its conditions until it was trading the tape again, which is exactly what failed four times. The floor for this campaign is therefore set at half the sessions rather than the usual seven in ten.

**That is not permission to act rarely.** Half of three hundred and eighty-four sessions is still most of them, and the way to reach it is the holding period rather than the signal count: a position opened at a level and carried toward the next one spans sessions, so a setup appearing once a week with a multi-session hold covers far more of the calendar than its signal count suggests. A rule that acts a handful of times and closes the same day will not be measurable here, and that outcome is the rule's fault and not the floor's.

## Three things about the data that will otherwise be got wrong

**The readable history is bounded.** Only a few hundred recent bars are pushed. A minute-level running total across a whole session is longer than that, and levels carried from two sessions ago are far longer. Anything that must outlive the window has to be accumulated as it arrives and kept.

**A session here runs from the prior evening through the following afternoon, as one unit.** Regular hours are a part of it and are not delivered separately. A profile built on regular hours only must select bars by time of day, and the clock shifts twice a year relative to the timestamps, which is a real way to build a level that is an hour wrong for half the sample.

**Flow with no attributed side exists and is not to be folded into either side.** On this data it is negligible, which is a fact to verify rather than assume.

## Where the stop goes, and why that is not the agent's to tune

**The stop sits just beyond the absorption bar's own extreme** — past its high for a short, past its low for a long — by a small buffer. The buffer must be **stated once and constant across every trade**, not chosen per setup. If the level has any meaning, the price that failed to go through it is the price that invalidates the idea, and the stop belongs there rather than at a distance chosen because it scored well.

This removes the parameter most likely to be fitted. The previous family's best rule earned its result through stop placement: its ratio of average win to average loss was good on the sessions it was built on, and when the same rule met fresh sessions the average loss roughly tripled while the win rate barely moved. The rule had learned where to put a stop on that particular sample. A structural stop cannot learn that, because the market puts it there.

**The reward-to-risk ratio is yours.** Express the target as a multiple of the stop distance, state the multiple, and report it. A tight target that wins often and a distant one that wins seldom are different claims about what happens after absorption, and either is a real answer.

**Two consequences to handle rather than discover.**

Stop distance now varies with the size of the absorption bar, and contracts are whole numbers — there is no fractional size with which to equalise risk across setups, and no size below one. So risk per trade varies unless the rule declines setups whose stop is wider than some stated bound. Choose, state which, and say what it cost in setups forgone.

And waiting for initiation widens the stop. The anchor stays at the absorption bar while the entry moves away from it, so a confirming bar bought later is bought with a worse price and a wider stop at once. **That is part of what the initiation question is asking**, and the comparison is not honest unless the stop distance at entry is reported for both.

## Why it might be true, and why it might not

It might be true because a volume node is where size actually traded, and size that traded there once is a plausible reason for size to be there again — which is a mechanism, not a pattern.

It might not be true because four attempts at order flow here have produced nothing, because a level drawn from past volume is visible to everyone looking at the same tape, and because absorption is defined by the absence of a move, which makes it easy to find after the fact and hard to act on before.

## What is yours to choose

Which levels to carry and from which sessions. How a level's width is defined. What counts as arriving at one. How much flow, and how far past the previous bar's extreme, makes absorption. Whether to wait for initiation, and what efficiency and body size define it. Direction, entry, exit, the reward-to-risk multiple, size, the bound beyond which a setup is declined for too wide a stop, and how long to hold.

## What is NOT yours to choose

**It must condition on a level built from traded volume.** A rule that would behave the same with the levels deleted is answering a different question.

**It must condition on flow, not only on price.** Absorption is a statement about flow meeting price. A rule that reads only the bar's shape is a candlestick rule.

**It must hold for a move between levels.** A rule doing many round turns a session for small moves has already been tried four times and is not what this asks.

**It must report what waiting for initiation cost or earned**, separately from what the rule made, including the stop distance at entry both ways.

**The stop is structural and the buffer is constant.** A stop placed at a distance that is tuned, or varied per setup for any reason other than where the absorption bar ended, is answering a question about stop placement.

## What would make the answer interesting

A direction that is consistent across the ordinary run of sessions it acts on, that survives being filled at worse prices than the signal generated, that does not rest on one month or one weekday, and whose answer to the initiation question is the same whichever way the holding period is set. Either answer is worth having: that absorption at a carried level precedes a move away, or that it does not. What is not worth having is a rule that acted too seldom to read, or one whose result is really a measurement of how long it held.

## What this is not asking for

Not the order book — there is no depth, no resting size and no quote in this data, and a rule that needs them cannot be built. Not a search across instruments. Not a model that chooses between the two accounts by fitting which applies when. Not a scalping rule, at any frequency, for any reason.

## Why this one

Order flow has failed here four times and the honest prior is that it will fail again. What justifies a fifth is that the four failures are now understood as a cost problem rather than a signal problem, and this is the first version of the question whose shape — a level, a held move, and a declared tolerance for acting less often — is built around that finding instead of ignoring it.
