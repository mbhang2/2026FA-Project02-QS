"""CVD continuation on the OKX BTCUSDT perpetual — campaign: "BTC aggressive
flow continues: cumulative volume delta predicts the next move".

The claim under test
--------------------
Aggressive buying that outweighs aggressive selling on the BTC perpetual is
followed by higher prices, and aggressive selling by lower ones. The sign of the
accumulated flow is the sign of the position. This file follows the flow; it does
not fade it — a fade would test the opposite claim, and the campaign named the
direction.

Where sixteen trials have landed
--------------------------------
#0 (1h, 6h flow, 6h hold, rolling quantile) made 1,196 fills for -0.1415; at
10 bp a round trip its fee bill was ~60% of notional and its gross was ~0. #1 was
genuinely negative gross. #2/#3 cut the trade count and were refused for the
exposure floor. #4 (1h, persistent position on a 48h flow ratio, z-scored against
its own 30-day dispersion, entry |z|>0.5, flip |z|>1.0) +0.1365 with 161 fills.
#5 (72h window) +0.0201. #6 (12/24/48 consensus) +0.0864. #7 (flip 1.25 plus a
12-bar minimum hold) +0.0778. #8 — #4 evaluated on 15-minute bars, every band
untouched — **+0.1677 with 167 fills**, still the best. #9 (1h, flip 0.75)
+0.1545. #10/#11 (15m, flip 0.75) +0.1269 with 252 fills. #12 (15m, 48-bar
geometric kernel) +0.0498 with 310 fills. #13 (15m, 64-bar kernel, mean age held
at #8's) +0.0898 with 227 fills. #14/#15 (5m, everything held in hours) +0.1412
and +0.1381 with 207 and 220 fills.

What that record actually settles
---------------------------------
Four axes are closed by direct measurement, and I will not spend another trial
on any of them:

* **The shape.** A continuously held long-or-short clears the 70% exposure floor
  by construction; a flip that takes +n straight to -n in one order spends no bar
  flat and carries no half-filled state. #2/#3 failed the floor by tuning a trade
  rate. Nothing since has.
* **The horizon.** 6h gross-negative (#0/#1), 48h best (#4/#8), 72h far worse
  (#5). Closed from both ends.
* **The estimator's memory.** Both smoothings (#12, #13) raised the fill count by
  36-85% and scored materially worse: time-decay is recency without persistence,
  and the crossings it admits are whipsaw. The hard 48-hour boxcar's sharp exit
  is doing real work, not acting as an artefact.
* **The cadence.** 1h +0.1365, 15m +0.1677, 5m +0.1412. The peak is 15 minutes
  and it is a low peak. And the arithmetic on the 5m step is the useful part: 17
  extra flips moved the score from +0.1677 to +0.1412, which prices them at
  about **-0.5 bp of gross each** — they were not merely diluted, they were
  worthless. The finer clock did not buy earlier action on the same flips; it
  bought flips that should not have happened.

The band is a fifth closed axis, but for a different reason: 0.75/1.0/1.25 across
two cadences span +0.078 to +0.168 with the ordering *reversing* between 1h and
15m. Four configurations that differ only in the band average +0.146 with a
spread of 0.041. That spread is what sampling error over ~85 flips looks like,
and moving along it again would be measuring noise and calling it a parameter.

So the family is at a measured optimum on every axis it has. What follows has to
be a new axis, and it should be one that explains the only hard number in the
record: **the extra flips a finer clock admits are worthless.**

The diagnosis: a band crossing is not necessarily news
------------------------------------------------------
`ratio = sum(delta)/sum(volume)` over a hard 48-hour boxcar is a *rolling sum*.
It changes for two completely different reasons, and the strategy cannot tell
them apart:

1. **New flow arrives against the position.** This is news, and the claim says
   the market should keep going in the new direction.
2. **An old burst leaves the back of the window.** A big buying day 47 hours ago
   drops out, ordinary bars replace it, and the ratio falls through `-flip_z`
   without a single new sell print. This is arithmetic, not information.

Half of the window turns over every day, so reason (2) is not a rare corner: the
48h boxcar is a lagging statistic whose crossings are substantially *mechanical*.
A crossing caused by forgetting is exactly the flip that should not be taken, and
it is exactly the flip that a finer sampling clock sees more of — a 5-minute
sampler watches the sum decay across the band, while a 15-minute sampler may skip
straight over the region where the decay alone was enough. That is a coherent
explanation for the -0.5 bp marginal flip at 5m.

The one change: a reversal must be visible in recent flow
---------------------------------------------------------
Keep every one of #8's choices — the 15-minute clock, the 48-hour boxcar, the
ratio `sum(delta)/sum(volume)`, the 30-day dispersion scale, entry |z| > 0.5,
flip |z| > 1.0, market orders on flips only, one lot, the 15% catastrophe stop,
the 24-hour cooldown — and add a single condition to the **flip**:

    flip to short only if  z < -flip_z  AND  the last 6 hours of flow are net selling
    flip to long  only if  z >  flip_z  AND  the last 6 hours of flow are net buying

Six hours is `fast_window = 24` bars at 15 minutes. Nothing else uses the fast
window; it is a veto, never a signal.

Why this and not something else
-------------------------------
* **It targets the one failure the record measured.** The 5m trial said extra
  flips are worthless. This removes flips rather than adding them, and it removes
  a specific, mechanically identifiable class: crossings the recent flow does not
  corroborate. If that class is where the worthless flips live, the average flip
  gets better and the count falls a little. If it is not, the count barely moves
  and the trial has cost one round of sampling noise to say so.
* **It is a gate, not an estimator.** The 6-hour flow is not smoothed into the
  signal, is not scaled, and is not the primary statistic. The 48-hour boxcar
  remains the whole of the signal — which matters, because the two smoothing
  experiments already showed what happens when recency is allowed into the
  estimator. Recency as a *veto on the most expensive action* is a different
  object from recency as a *weight on the signal*, and the failure of #12/#13
  does not transfer.
* **It is one new number, on a new axis.** `fast_window = 24` bars. The bands,
  the window, the cadence, the decay and the state machine are all held at the
  best-scoring values the record contains, so a difference here is attributable
  to the gate.
* **The cost is bounded and small.** A blocked flip leaves the position on for at
  most the time it takes recent flow to turn — typically a few hours on an
  11.5-day average hold. The benefit is not paying 10 bp for flips the window
  manufactured. At 83 flips, removing even five worthless ones is worth more than
  the delay on the rest.

What this deliberately does *not* do
------------------------------------
The gate is applied to flips only. **Entries from flat are ungated**, so exposure
is unaffected: the first actionable bar enters on `|z| > 0.5` and the strategy is
continuously long-or-short thereafter, which is the property that clears the 70%
exposure floor by construction. Gating entries would put the floor at risk for no
reason — the floor is the one constraint this family has already failed twice.

The rest is #8, unchanged, and deliberately so
---------------------------------------------
* **Measurement.** `ratio = sum(bar.delta) / sum(bar.volume)` is cumulative volume
  delta made dimensionless: the fraction of the window's aggressive volume that
  was net buying, minus net selling. Dividing by the window's own volume is the
  campaign's "how to measure large" answered once — 500 BTC of net buying is a
  different event in a quiet Asian morning and at a US open. `unknown_volume` is
  zero on this feed (every trade carries its aggressor and the run facts say so),
  so `bar.delta` is the exact signed flow, there is no unknown branch to decide,
  and no tick subscription is needed: ticks cost roughly five times the wall
  clock for a number the bar already carries exactly.

* **"Large" is standardised.** `z = ratio / sd(ratio)` over the trailing 30 days,
  needing 7 days before a scale exists. A fixed cut-off on a 24/7 asset is a bet
  on the volatility regime of the year it was chosen in. The cut is on the
  **level** of the ratio, not its deviation from a trailing mean: the claim is
  that one-sided flow predicts, and a month of persistent net buying must not be
  relabelled "not unusual" merely because it is the local average.

* **Hysteresis between entry and flip.** Opening from flat needs |z| > 0.5;
  reversing needs |z| > 1.0. A flip pays two fees and throws away a position the
  flow has not yet contradicted, so it demands a more decisive reversal. The gap
  is what keeps the position on almost always without ever paying to sit out.

* **Market orders, and only on flips.** The fill model is book-queue-pessimistic:
  a resting limit joins behind the displayed size and fills only when trades reach
  through it, which on a continuation entry selects precisely the cases where the
  expected move did not arrive. Paying the touch for a fill that actually happens
  is the cheaper error, and 100 ms of latency against a 15-minute decision
  interval is a rounding error.

* **Risk is a backstop, not a rule.** A close-of-bar check at 15% of adverse
  excursion from the average entry, held in code rather than armed as an engine
  order, so no bracket validation, DAY expiry or OCO event can end the run and no
  order id is carried across a callback where an expiry could make it stale. It
  is meant to be crossed by a crash, not by ordinary noise; afterwards the
  strategy stands aside for a day and re-enters on a fresh reading.

* **Neither estimator gate can silently make the run idle.** A reading forms once
  the flow window is full; once a ratio exists but the dispersion sample is still
  short, an undefined `z` is read as maximal in the sign of `ratio` rather than as
  a reason not to act. An absent yardstick for "large" is an estimator warm-up,
  not a market state, and gating on one silently is how an earlier draft of this
  family made no trades at all on a short walk. On the real split the 9-session
  requirement is long past by the first actionable bar, so the branch is inert.

* **Bookkeeping is O(1) per bar and bounded.** Two deques with running sums (192
  and 24 elements) and running dispersion over a bounded 2,880-element deque with
  a periodic exact re-sync. Nothing rescans history — there is no `ctx.history`
  read at all, so the 512-bar pushed window cannot be overrun — and nothing grows
  across sessions.

Position size is one lot, unchanged. Fees, funding and P&L are all linear in size,
so the result is scale-free and the smallest tradable size keeps the trial's risk
honest while the statistics accumulate; keeping it identical is also the only
reason a difference of a few hundredths can be read against the fifteen trials
before it.

What is deliberately absent: a tick subscription, any fade, any book-based input,
a regime model, a second instrument, a target or bracket, a fixed-horizon exit,
and any change to the window, the bands, the cadence or the state machine, all of
which the record has closed.

One thing I want on the record about the whole family
-----------------------------------------------------
A 48-hour flow ratio gives roughly 83 flips over 960 sessions, an average hold of
11.5 days, and a gross edge per flip near 30 bp against an 11.5-day price move
with a standard deviation near 8%. The per-trial t-statistic of the family's edge
is around 0.3, and the spread of the sixteen scores — +0.02 to +0.17 among
configurations that differ only in parameters the record calls flat — is the same
size as the differences being read. I am running this trial because it is a
mechanism with a measured target, not because I believe +0.1677 is signal. If it
lands within a few hundredths of #8, the right conclusion is not "the gate works
a little"; it is that this family's parameter surface has no direction left in it
and the campaign's answer is the one #8 already gave.

Outcomes and what they would mean
---------------------------------
**Above #8 with fewer than ~167 fills:** the boxcar's mechanical crossings were a
real cost, the freshness gate found them, and the next step is the same idea at a
finer clock — 5m plus this gate, which the 5m trial did not have. **Within noise
of #8 with a similar fill count:** the gate almost never binds, ratio and recent
flow agree at the moment of a flip, and the family is complete. **Materially below
#8, or a fill count well below 167:** the crossings being vetoed were genuine
reversals and 12 hours of recent flow is too slow a corroboration to be useful —
the delay cost more than the saved fees, which would close the freshness axis and
say that the 48-hour window's dynamics are best taken at face value.
"""

from __future__ import annotations

import math
from collections import deque


class Strategy:
    """CVD continuation, with a flip vetoed unless recent flow corroborates it.

    #8's statistic, #8's bands, #8's cadence and #8's state machine. The only
    addition is that a reversal of a held position must also be visible in the
    last six hours of flow, so a band crossing produced by an old burst leaving
    the back of the 48-hour boxcar is not traded.
    """

    def __init__(self, interval="15m", flow_window=192, fast_window=24,
                 sd_window=2880, min_sd_obs=672, entry_z=0.5, flip_z=1.0,
                 size=1, stop_bps=1500.0, cooldown_bars=96):
        self.interval = interval            # 15m: #8's cadence, the measured peak
        self.flow_window = flow_window      # 192 bars = 48 hours of aggressor flow
        self.fast_window = fast_window      # 24 bars = 6 hours, veto only
        self.sd_window = sd_window          # 2880 bars = 30 days of ratio history
        self.min_sd_obs = min_sd_obs        # 672 bars = 7 days before a scale exists
        self.entry_z = entry_z              # |z| needed to open from flat
        self.flip_z = flip_z                # |z| needed to reverse an open position
        self.size = size
        self.stop_bps = stop_bps            # catastrophe stop, basis points
        self.cooldown_bars = cooldown_bars  # 96 bars = 24 hours flat after a stop

        # Rolling 48h flow window with running sums: (delta, volume) per bar.
        self.flow = deque()
        self.sum_delta = 0.0
        self.sum_volume = 0.0

        # Rolling 6h window. Used only to veto a flip, never as the signal.
        self.fast = deque()
        self.fast_delta = 0.0
        self.fast_volume = 0.0

        # Rolling distribution of the raw ratio: running sums, bounded deque.
        self.ratios = deque()
        self.sr = 0.0
        self.sr2 = 0.0
        self._bars = 0

        self.dir = 0                        # last known position direction
        self.cooldown = 0

    # ------------------------------------------------------------------ setup

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    # ------------------------------------------------------------------- bars

    def on_bars(self, ctx):
        # One subscription, but ask rather than assume: a bar from another
        # interval closing at this instant must not be mistaken for ours.
        if self.interval not in ctx.bars:
            return
        bar = ctx.bars[self.interval]

        delta = float(bar.delta)
        volume = float(bar.volume)

        # --- 48h flow window, O(1) ------------------------------------------
        if len(self.flow) >= self.flow_window:
            old_delta, old_volume = self.flow.popleft()
            self.sum_delta -= old_delta
            self.sum_volume -= old_volume
        self.flow.append((delta, volume))
        self.sum_delta += delta
        self.sum_volume += volume

        # --- 6h freshness window, O(1) --------------------------------------
        if len(self.fast) >= self.fast_window:
            old_delta, old_volume = self.fast.popleft()
            self.fast_delta -= old_delta
            self.fast_volume -= old_volume
        self.fast.append((delta, volume))
        self.fast_delta += delta
        self.fast_volume += volume

        if len(self.flow) < self.flow_window or self.sum_volume <= 0.0:
            return
        ratio = self.sum_delta / self.sum_volume

        # The scale accumulates through warmup as well: those bars are real, only
        # the orders would not be, and reaching the first actionable bar with no
        # idea what "large" means here would waste the warmup.
        self._push_ratio(ratio)
        self._bars += 1
        if self._bars % 2048 == 0:
            self._resync()

        if not ctx.actionable:
            return
        if ctx.working_order_ids:
            return

        if self.cooldown > 0:
            self.cooldown -= 1

        position = ctx.position
        if position > 0:
            self.dir = 1
        elif position < 0:
            self.dir = -1
        else:
            self.dir = 0

        # ---- catastrophe stop: insurance, checked on closes -----------------
        if position != 0:
            entry = ctx.average_entry_price
            price = ctx.last_price
            if entry > 0.0 and price > 0.0:
                if position > 0:
                    adverse = entry - price
                else:
                    adverse = price - entry
                if adverse > entry * self.stop_bps / 10000.0:
                    ctx.close()
                    self.dir = 0
                    self.cooldown = self.cooldown_bars
                    return

        z = self._z(ratio)
        if z is None:
            # No dispersion estimate yet. `ratio` is a number the data supplied;
            # what is missing is only the yardstick for "large". With no yardstick
            # the sign of the flow is the whole of the evidence, so read it as
            # maximal in that direction rather than declining to act -- an
            # undefined scale is an estimator warm-up, not a market state.
            if ratio == 0.0:
                return
            z = 2.0 if ratio > 0.0 else -2.0

        # Recent flow, the veto. Zero volume in the fast window means no
        # reading, which vetoes nothing.
        fresh = 0.0
        if self.fast_volume > 0.0:
            fresh = self.fast_delta / self.fast_volume

        # ---- already positioned: hold unless the flow has reversed hard ------
        # A flip needs the 48h band crossed AND the last six hours to be on the
        # new side. The band can be crossed by an old burst falling out of the
        # back of the window with no new opposing flow at all; that crossing is
        # arithmetic, not information, and it is not traded.
        if self.dir == 1:
            if z < -self.flip_z and fresh < 0.0:
                # One order takes the book from +position to -size: no bar spent
                # flat, and no half-applied flip if only the close had filled.
                if ctx.sell(size=position + self.size,
                            tag="cvd-flip-short") is not None:
                    self.dir = -1
            return
        if self.dir == -1:
            if z > self.flip_z and fresh > 0.0:
                if ctx.buy(size=(-position) + self.size,
                           tag="cvd-flip-long") is not None:
                    self.dir = 1
            return

        # ---- flat: open with the sign of the flow, ungated -------------------
        # Entries are deliberately not vetoed: exposure is what clears the 70%
        # floor by construction, and a gate here would put it at risk for nothing.
        if self.cooldown > 0:
            return
        if z > self.entry_z:
            if ctx.buy(size=self.size, tag="cvd-long") is not None:
                self.dir = 1
        elif z < -self.entry_z:
            if ctx.sell(size=self.size, tag="cvd-short") is not None:
                self.dir = -1

    # ------------------------------------------------------------------ fills

    def on_fill(self, ctx):
        # Keep the direction in step with what actually happened; the flip is a
        # single order, so there is nothing else to reconcile and no order id is
        # held across callbacks for an expiry to make stale.
        position = ctx.fill.position
        if position > 0:
            self.dir = 1
        elif position < 0:
            self.dir = -1
        else:
            self.dir = 0

    def on_lifecycle(self, ctx):
        pass

    # ---------------------------------------------------------------- helpers

    def _push_ratio(self, value):
        if len(self.ratios) >= self.sd_window:
            old = self.ratios.popleft()
            self.sr -= old
            self.sr2 -= old * old
        self.ratios.append(value)
        self.sr += value
        self.sr2 += value * value

    def _resync(self):
        # Rolling sums drift after tens of thousands of updates; re-deriving them
        # from the bounded deques is cheap and exact.
        self.sr = 0.0
        self.sr2 = 0.0
        for value in self.ratios:
            self.sr += value
            self.sr2 += value * value
        d = 0.0
        v = 0.0
        for bar_delta, bar_volume in self.flow:
            d += bar_delta
            v += bar_volume
        self.sum_delta = d
        self.sum_volume = v
        d = 0.0
        v = 0.0
        for bar_delta, bar_volume in self.fast:
            d += bar_delta
            v += bar_volume
        self.fast_delta = d
        self.fast_volume = v

    def _z(self, ratio):
        n = len(self.ratios)
        if n < self.min_sd_obs:
            return None
        mean = self.sr / n
        var = self.sr2 / n - mean * mean
        if var <= 0.0:
            return None
        sd = math.sqrt(var)
        if sd < 1e-12:
            return None
        return ratio / sd
