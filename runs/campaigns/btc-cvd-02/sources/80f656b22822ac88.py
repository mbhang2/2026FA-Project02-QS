"""CVD continuation on the OKX BTCUSDT perpetual — campaign: "BTC aggressive
flow continues: cumulative volume delta predicts the next move".

The claim under test
--------------------
Aggressive buying that outweighs aggressive selling on the BTC perpetual is
followed by higher prices, and aggressive selling by lower ones. The sign of the
accumulated flow is the sign of the position. This file follows the flow; it
does not fade it — a fade would test the opposite claim and this campaign named
the direction.

Where eighteen trials have landed
---------------------------------
#0 (1h, 6h flow, 6h hold, rolling quantile) made 1,196 fills and scored -0.1415;
at 10 bp a round trip the fee bill was the whole of the loss. #1 was genuinely
negative gross. #2/#3 cut the trade count and were refused for the exposure
floor (251 and 392 of 960 scored sessions against a registered 70%). #4 — a
persistent long-or-short on the sign of a 48-hour **boxcar** flow ratio,
z-scored against its own trailing 30-day dispersion, entry |z| > 0.5, flip
|z| > 1.0, evaluated hourly — traded 161 fills for **+0.1365**. #5 (72h window)
+0.0201 with 133 fills. #6 (12/24/48 consensus) +0.0864 with 264. #7 (1h, flip
1.25 plus a 12-bar minimum hold) +0.0778 with 127. #8 — **#4 evaluated on
15-minute bars, every band untouched — +0.1677 with 167 fills**, still the best
of eighteen. #9 (1h, flip 0.75) +0.1545 with 221. #10/#11 (15m, flip 0.75)
+0.1269 with 252. #12 (15m, 48-bar-half-life geometric flow) +0.0498 with 310.
#13 (15m, 64-bar half-life) +0.0898 with 227. #14 (5m, everything held in hours)
+0.1412 with 207. #15 (5m, tighter warm-up gate) +0.1381 with 220. #16 (15m
plus a 6-hour flow-freshness veto on flips) +0.1513 with 169. #17 (5m plus a
three-bar persistence requirement) +0.1577 with 169.

What the record actually settles
--------------------------------
Four axes are closed by direct measurement and I will not move along any of them
again:

* **The shape.** A continuously held long-or-short clears the 70% exposure floor
  by construction; a flip that takes +n straight to -n in one order spends no bar
  flat and carries no half-filled state. #2/#3 failed the floor by tuning a trade
  rate; nothing since has.
* **The horizon.** 6h gross-negative (#0/#1), 48h best (#4/#8), 72h far worse
  (#5). Closed from both ends.
* **The estimator's memory and shape.** Both smoothings tried raised the fill
  count by 36-85% and scored materially worse (#12, #13). The hard 48-hour boxcar
  is doing real work.
* **The band.** 0.75 / 1.0 / 1.25 across two cadences span +0.078 to +0.168 with
  the ordering *reversing* between 1h and 15m. That is sampling error over ~180
  flips, not a parameter.

And the record contains one regularity that is stronger than any of those
"effects" and that I have not been treating as the constraint it is:

    fills   127   133   161   167   169   207   220   221   252   264   310
    score  .078  .020  .137  .168  .151  .141  .138  .155  .127  .086  .050

Read as a curve it is an inverted U peaking around 165-170 fills. #8 sits on the
peak, and *every* change that has scored worse than #8 — the vetoes as much as
the bands — moved the trade rate off it, in one direction or the other. That is
why the two refinements that held the count at 169 (#16, #17) still came in at
+0.151: they kept the rate and changed which flips were taken, and the selection
they imposed was, if anything, worse. The fee arithmetic says the same thing
from the other side. Over 960 sessions the family turns over roughly 167 x 2 lots
= 334 lots; at 5 bp a side that is a fixed 16.7% of notional in costs, and the
score is of the same order. Trade less and the effect is under-sampled and the
sampling error swamps it; trade more and the marginal flip is worth less than
the 10 bp it costs — measured at about -0.5 bp each on the 5m step.

So the constraint is now explicit: **hold the trade rate at the measured optimum
and change what is being measured, not how often it is read.**

The one thing in the signal that is unambiguously not the claim
---------------------------------------------------------------
`ratio = sum(bar.delta) / sum(bar.volume)` over a hard 48-hour boxcar treats every
bar as equally informative per unit of volume. That is right for ordinary flow.
It is wrong for the one kind of bar this market produces that the campaign itself
names as a threat to the effect:

    "Crypto flow can also be dominated by liquidations, which may exhaust
     themselves quickly."

A liquidation cascade is a handful of bars carrying ten to fifty times the
window's ordinary volume, all of it on one side. In a volume-weighted sum it is
not a data point, it is the whole statistic: a single fifteen-minute window can
be 30-50% of the 48-hour total and can pin the ratio for two days. That matters
for two reasons.

1. **It is the component of flow least likely to continue.** A forced seller is
   not expressing a view; they are being closed out. The impact of that flow
   reverses when the cascade ends, which is precisely the "impact can be
   temporary" failure mode the campaign flags. If the 48-hour ratio is driven by
   the cascade rather than by the accumulation, the sign of the position is
   being set by the least persistent part of the flow.
2. **It is the component that makes the estimator non-robust.** The z-score is
   ratio/sd, and both the numerator and the 30-day sd are dominated by the same
   few bars, so a single cascade moves the signal and the yardstick together in
   a way that has nothing to do with the persistence of ordinary aggressive flow.

The change: winsorise each bar's contribution to the window
-----------------------------------------------------------
Keep every one of #8's choices — the 15-minute clock, the 192-bar (48-hour)
window, the ratio `sum(delta)/sum(volume)`, the 30-day dispersion scale with a
7-day minimum, `entry_z = 0.5`, `flip_z = 1.0`, market orders, one lot, the 15%
catastrophe stop, the 24-hour cooldown — and add exactly one computation:

    before a bar enters the window, let  avg = sum_volume / len(window)
    and  cap = vol_cap_mult * avg.  If the bar's volume exceeds cap, scale
    BOTH its delta and its volume by  w = cap / volume  before adding them.

Both sums are scaled by the same factor, so the bar keeps its *sign and its
direction* — it simply stops counting for more than `vol_cap_mult` times an
ordinary bar. Nothing is dropped and nothing is clipped asymmetrically, so the
estimator remains an unbiased weighted average of order imbalance; it is the
influence of the largest few bars that is bounded. In a quiet window `w == 1` for
every bar and this file is bit-for-bit #8; the change bites only where a
liquidation cascade or a single block print would otherwise have dominated two
days of signal.

`vol_cap_mult = 6.0` is chosen so the cap sits above the ordinary right tail of
15-minute BTC volume and below a cascade. The 48-hour mean of 192 bars is a
stable baseline and costs nothing to maintain — it is a division of sums that are
already being kept, not a new structure, and no percentile or sorted container is
required.

Why this and not something else
-------------------------------
* **It attacks the failure the campaign itself names.** The case against the
  effect is that impact is temporary because liquidity providers absorb pressure,
  and that crypto flow can be dominated by liquidations that exhaust. Winsorising
  the flow window is the minimal way to ask whether the effect survives when the
  exhaustion-prone component is bounded, without changing what is being
  predicted.
* **It is a robustness change, not a new signal.** No new estimator, no new
  band, no new state, no veto, no derivative, no second window. The statistic is
  the same statistic computed on a less leveraged sample. #12/#13 failed because
  they let recency into the estimator; this lets nothing in.
* **It is designed to hold the one number the record says matters.** Winsorising
  shrinks the extreme values of `ratio`, and `z = ratio / sd` is a ratio of two
  quantities that both shrink, so the crossing rate should move by only a few
  percent — I expect 150-180 flips, from #8's 167. If instead the count collapses
  or explodes, that alone is the finding, because it would mean the 48-hour
  statistic is made of cascades.
* **It is nearly free to be wrong about.** In the limit where no bar is ever
  capped, the file is #8 and the trial costs one round of sampling noise to say
  that liquidations were never the issue. Given the measured spread of ~0.04
  across configurations that differ only in closed parameters, that is the
  cheapest possible form of an experiment here.

The rest is #8, unchanged, and deliberately so
---------------------------------------------
* **Measurement.** Over the last `flow_window` bars, `ratio = sum(delta) /
  sum(volume)` — cumulative volume delta made dimensionless: the fraction of the
  window's aggressive volume that was net buying, minus net selling. Dividing by
  the window's own volume is the campaign's "how to measure large" answered once:
  500 BTC of net buying is a different event in a quiet Asian morning and at a US
  open. `unknown_volume` is zero on this feed — every trade carries its
  aggressor, and the run facts say so — so `bar.delta` is the exact signed flow,
  there is no unknown branch to decide, and no tick subscription is needed: ticks
  cost roughly five times the wall clock for a number the bar already carries
  exactly.

* **"Large" is standardised.** `z = ratio / sd(ratio)` over the trailing 30 days
  of ratio observations, needing 7 days before a scale exists. A fixed cut-off on
  a 24/7 asset is a bet on the volatility regime of the year it was chosen in.
  The cut is on the **level** of the ratio, not its deviation from a trailing
  mean: the claim is that one-sided flow predicts, and a month of persistent net
  buying must not be relabelled "not unusual" merely because it happens to be the
  local average.

* **Hysteresis between entry and flip.** Opening from flat needs |z| > 0.5;
  reversing needs |z| > 1.0. A flip pays two fees and throws away a position the
  flow has not yet contradicted, so it demands a more decisive reversal. The gap
  is what keeps the position on almost always without ever paying to sit out,
  which is also what clears the exposure floor by construction.

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
  is meant to be crossed by a crash and not by ordinary noise; afterwards the
  strategy stands aside for a day and re-enters on a fresh reading.

* **Neither estimator gate can silently make the run idle.** A ratio forms once
  the flow window is full; once a ratio exists but the dispersion sample is still
  short, an undefined `z` is read as maximal in the sign of `ratio` rather than as
  a reason not to act. An absent yardstick for "large" is an estimator warm-up,
  not a market state.

* **Bookkeeping is O(1) per bar and bounded.** One deque of 192 pairs with running
  delta and volume sums, running dispersion over a bounded 2,880-element deque
  with a periodic exact re-sync, and one division per bar for the cap. Nothing
  rescans history — there is no `ctx.history` read at all, so the 512-bar pushed
  window cannot be overrun — and nothing grows across sessions. Roughly 92,000
  bar callbacks over the split, each a constant number of float operations.

Position size is one lot, unchanged. Fees, funding and P&L are all linear in size,
so the comparison against the eighteen trials before it is exact; changing the
size in the same trial that changes the estimator would confound the two.

What is deliberately absent: a tick subscription, any fade (the campaign names the
direction), any book-based input (not available on this adapter), a regime model
(the standardisation is a scale, not a state), a second instrument, a target or
bracket (a different claim — where the move stops), any fixed-horizon exit (the
horizon *is* the flow window and the position is held while the flow agrees), and
any change to the window, the bands, the cadence, the stop or the state machine,
all of which the record has already closed.

Outcomes and what they would mean
---------------------------------
**Above #8 with 150-180 fills:** the 48-hour statistic was being carried by its
largest bars, the ordinary-flow component predicts better on its own, and the next
step is to tighten `vol_cap_mult` until the fill count or the score turns — the
cap is the only new number and it has an obvious direction. **Within a few
hundredths of #8 with a similar count:** liquidations were never the driver, the
raw ratio is the right estimator at this horizon, and the campaign's answer is the
one #8 gave — aggressive flow continues, worth roughly 30 bp gross and 20 bp net
per flip, and no more. **A fill count far from 167, or a score materially below:**
the 48-hour ratio is largely a cascade statistic, bounding it removes the signal
rather than the noise, and that is worth knowing before any further work on the
estimator — it would say the family has been trading forced flow, not informed
flow.
"""

from __future__ import annotations

import math
from collections import deque


class Strategy:
    """CVD continuation on a winsorised flow window.

    #8's statistic, #8's bands, #8's cadence and #8's state machine. The only
    addition is that a bar whose volume is far above the window's mean is scaled
    down in *both* sums, so a liquidation cascade or a single block print cannot
    become the whole of a two-day order-imbalance reading.
    """

    def __init__(self, interval="15m", flow_window=192, sd_window=2880,
                 min_sd_obs=672, entry_z=0.5, flip_z=1.0, size=1,
                 stop_bps=1500.0, cooldown_bars=96, vol_cap_mult=6.0,
                 min_cap_obs=24):
        self.interval = interval            # 15m: #8's cadence, the measured peak
        self.flow_window = flow_window      # 192 bars = 48 hours of aggressor flow
        self.sd_window = sd_window          # 2880 bars = 30 days of ratio history
        self.min_sd_obs = min_sd_obs        # 672 bars = 7 days before a scale exists
        self.entry_z = entry_z              # |z| needed to open from flat
        self.flip_z = flip_z                # |z| needed to reverse an open position
        self.size = size
        self.stop_bps = stop_bps            # catastrophe stop, basis points
        self.cooldown_bars = cooldown_bars  # 96 bars = 24 hours flat after a stop
        self.vol_cap_mult = vol_cap_mult    # a bar counts for at most this many means
        self.min_cap_obs = min_cap_obs      # bars before the cap has a baseline

        # Rolling flow window with running sums: (delta, volume) per closed bar.
        self.flow = deque()
        self.sum_delta = 0.0
        self.sum_volume = 0.0

        # Rolling distribution of the raw ratio: running sums, bounded deque.
        self.ratios = deque()
        self.sr = 0.0
        self.sr2 = 0.0
        self._bars = 0

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

        # --- winsorise this bar's influence, O(1) ---------------------------
        # The 48-hour mean is a stable baseline; a bar carrying several times
        # that mean is a liquidation cascade or a block print rather than a
        # participant expressing a view, and it must not become the whole of
        # the window's reading. Both sums are scaled by the same factor, so the
        # bar keeps its direction and only loses its leverage.
        n = len(self.flow)
        if n >= self.min_cap_obs and self.sum_volume > 0.0 and volume > 0.0:
            cap = self.vol_cap_mult * (self.sum_volume / n)
            if volume > cap:
                weight = cap / volume
                delta *= weight
                volume *= weight

        # --- flow window, O(1) ----------------------------------------------
        if len(self.flow) >= self.flow_window:
            old_delta, old_volume = self.flow.popleft()
            self.sum_delta -= old_delta
            self.sum_volume -= old_volume
        self.flow.append((delta, volume))
        self.sum_delta += delta
        self.sum_volume += volume

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

        # ---- already positioned: hold unless the flow has reversed hard ------
        if position > 0:
            if z < -self.flip_z:
                # One order takes the book from +position to -size: no bar spent
                # flat, and no half-applied flip if only the close had filled.
                ctx.sell(size=position + self.size, tag="cvd-flip-short")
            return
        if position < 0:
            if z > self.flip_z:
                ctx.buy(size=(-position) + self.size, tag="cvd-flip-long")
            return

        # ---- flat: open with the sign of the flow ----------------------------
        if self.cooldown > 0:
            return
        if z > self.entry_z:
            ctx.buy(size=self.size, tag="cvd-long")
        elif z < -self.entry_z:
            ctx.sell(size=self.size, tag="cvd-short")

    # ------------------------------------------------------------------ fills

    def on_fill(self, ctx):
        # Nothing to reconcile: the direction is re-read from ctx.position on
        # every bar, and a flip is a single order, so no order id is held across
        # callbacks for an expiry or a cancel to make stale.
        pass

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
        # Rolling sums drift after tens of thousands of updates; re-deriving
        # them from the bounded deques is cheap and exact.
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
