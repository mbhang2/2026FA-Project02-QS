# Do Auction Market Theory features predict forward return on NQ?

Written 2026-09-22, before the campaign that uses it was registered. The first
hypothesis written for a **fitted** strategy, so it pins down things an unfitted
one never had to: the target, the horizon, and which rows are usable.

## Where this comes from, and what was changed

`data/papers/raw/2609.13825v1.json` — *ViperQ: Order Flow Pattern Recognition via
Auction Market Theory for Reinforcement Learning Trading* (Moustafa, Neagu,
Kalita, 2026). Its claim is that Auction Market Theory primitives are a tractable
structured input for sequential decision-making, against the literature's usual
OHLCV bars or book depth.

**The feature set is borrowed. The method is not.** ViperQ trains two PPO agents
against a prospect-theory reward. This hypothesis tests the *inputs* with a
supervised model and a plain forward-return target, for three reasons:

1. Reinforcement learning is deferred in `docs/design/ml-strategies.md` §8, and
   the fitted path is a single `fit()` at the fit-window edge.
2. A shaped reward that "penalises losing holds at a magnitude consistent with
   loss aversion" is the canonical reward-hacking setup. This project has already
   been bitten once (Amendment 3), and the gate scores Sortino, not the agent's
   reward — so an agent can maximise its reward and score badly.
3. Separating the question. If AMT features carry no signal, the RL result is not
   worth chasing. If they do, that is the finding, and the policy-learning
   question is a later and separate one.

**What the paper's numbers do not transfer.** +163.6% ROI on TSLA and +116.5% on
NVDA are equities, not futures, and the abstract reports "zero leverage" without
mentioning commission. 27,019 trades on TSLA is a lot of round turns to pay for.
Nothing here is an expectation about magnitude; the paper motivates the feature
set and nothing else.

## The question

Do Auction Market Theory features, computed on NQ from the trades feed, predict
the return over the next `H` bars?

**The features**, each from ViperQ's state vector and each computable from what
this feed carries:

- **Volume Point of Control** — the price level with the most traded volume so
  far in the session, and the current price's distance from it.
- **Value Area position** — whether price is inside or outside the band holding
  the central 70% of session volume, and on which side.
- **Low Volume Node flags** — whether price is at a level the session has traded
  through thinly.
- **Cumulative Volume Delta divergence** — cumulative signed flow rising while
  price does not, or the mirror. This is the absorption idea in
  `orderflow-absorption.md`, measured cumulatively rather than bar by bar.
- **Tape velocity** — trades and volume per unit time against the session's own
  recent rate.

The first three need volume-at-price within a session. That is a volume profile,
and `src/core/features/volume_profile.py` computes one: `SessionProfile` takes
`(price, size)` incrementally and answers `vpoc()`, `value_area()` and
`low_volume_nodes()` from what it has been given so far. Delta and cumulative
delta come directly from `bar.delta`; unknown-aggressor volume is in
`bar.unknown_volume` and belongs in neither side.

**What feeds it is an open question, and it is part of this hypothesis rather
than of a strategy.** A profile built at a different resolution is a different
feature, so the choice is registered here and not made inside `current.py`.

- **Ticks** are exact. `ctx.subscribe("tick")` delivers every trade, and on NQ
  that is roughly 369,000 callbacks a session -- 141.6M across the split, each
  one a pipe round-trip to the strategy's process. Whether that fits
  `nq-train-fitted`'s 7,200s budget is **unmeasured**.
- **Fine time bars** are cheaper and nearly as good. Any interval is
  subscribable, including sub-second, and each bar carries `high`, `low`,
  `volume` and the signed split. At one second an NQ bar spans a price level or
  three, so distributing its volume across its own range is close to the true
  histogram rather than a crude stand-in. At thirty seconds it is not.

**Queued before this campaign is registered:** build the profile from ticks, 1s,
5s and 30s bars over a handful of real NQ sessions; compare VPOC, Value Area
bounds and LVN locations against the tick-built version as ground truth; and time
each through the real remote boundary against the budget. If ticks fit, use
ticks -- there is no reason to approximate something computable exactly. If they
do not, the measurement names the cheapest resolution indistinguishable from
them, and **that number is written into this file** before trial 0.

## The target, and why not swing points

**Forward return over a fixed horizon `H`**, not the session high or low and not
a binary swing-point flag.

A level is not a decision — knowing the session high will be 21050 does not say
when, or whether to be short now. Swing labels are defined by an *unbounded*
forward window (how far must price fall to confirm a high?), which makes the
usable-row boundary impossible to size. And accuracy is not P&L: a 60%-accurate
swing detector loses money if the 40% it misses are larger. Forward return is
directly tradeable, has a bounded horizon, carries no class-imbalance problem,
and is aligned with the Sortino the gate actually scores.

`H` is a hyperparameter, so each value tried is its own trial under Amendment 4.

## Which rows are usable

Per §2f of `docs/design/ml-strategies.md`: a row whose label or features would
need a bar the strategy does not hold is NaN and is dropped. That drops the first
rows of the fit window, where lookback features cannot be computed, and the last
`H`, whose labels would need bars from the scored window.

**NaN must not become 0.** `fit()` asserts the count of usable rows is close to
what the window and horizon imply; a fabricated zero label trains the model toward
flat and nothing else would catch it.

## What would make the answer interesting

That it survives costs. This is the hurdle the order-flow campaigns died on, and
a model firing often is a model paying commission often. Entry, exit and holding
are part of the claim, not an afterthought applied to a signal.

That it beats the unfitted baseline. `nq-orderflow-01` and `-02` characterised
absorption on this instrument without a model. A fitted strategy that does no
better is evidence the fitting is not earning its complexity.

And that the result is legible. A model whose weights say which features it
leaned on is worth more here than a slightly better opaque one, because the
deliverable is the loop and its gate, not the strategy.

## What this is not asking for

- **Not reinforcement learning.** §8 defers it, and the reward-shaping question
  is exactly the one this project is trying to be careful about.
- **Not a deep network.** §9 records what the evidence says about deep models on
  this kind of data. Start where the coefficients are readable.
- **Not book or quote features.** The feed is trades with an aggressor side.
- **Not a multi-instrument study**, and not a regime model.

## What to expect, so a thin result is not misread

The scored window is 100 sessions rather than 384 (Amendment 4), which is roughly
**1.96×** the sampling noise of an unfitted trial. A fitted campaign should be
expected to accept less often for that reason alone. That is arithmetic about the
window, not a verdict on the features.
