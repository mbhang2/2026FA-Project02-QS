"""CVD continuation on the OKX BTCUSDT perpetual — campaign: "BTC aggressive
flow continues: cumulative volume delta predicts the next move".

The claim under test
--------------------
Aggressive buying that outweighs aggressive selling on the BTC perpetual is
followed by higher prices, and aggressive selling by lower ones. The sign of the
accumulated flow is the sign of the position. This file follows the flow; it does
not fade it. If continuation is wrong at this horizon the score says so, and
rotating to the other side would answer a different question.

Where fourteen trials have landed
---------------------------------
#0 (1h bars, 6h flow, 6h hold, rolling quantile) made 1,196 fills and scored
-0.1415; at 10 bp a round trip the fee bill was the whole of the loss, so its
gross edge was about zero. #1 (short hold, wide bracket) was genuinely negative
gross. #2 and #3 cut the trade count and were refused for the exposure floor (251
and 392 of 960 scored sessions against a required 70%). #4 — a persistent
position held on the sign of a 48-hour rectangular flow window, z-scored against
its own trailing 30-day dispersion, entry at |z| > 0.5, flip at 1.0 — traded 161
times and scored +0.1365. #5 (72-hour window) +0.0201. #6 (12/24/48 consensus)
+0.0864. #7 (flip 1.25 **and** a 12-bar minimum hold, two changes at once)
+0.0778. #8 — #4 evaluated on 15-minute bars with every band untouched —
**+0.1677** with 167 fills, still the best. #9 (hourly, flip 0.75) +0.1545. #10
(15m, flip 0.75) +0.1269 with 252 fills; #11 was the same file edited invisibly.
#12 (15m, geometric decay, 48-bar half-life) +0.0498 with **310** fills; #13
(15m, geometric decay, 64-bar half-life, mean data age held at #8's 96 bars)
+0.0898 with **227** fills.

What the record settles, and what it does not
---------------------------------------------
Three things are settled and I will not spend trials on them again:

* **The shape.** A continuously held long-or-short clears the exposure floor by
  construction, and a flip that takes +n straight to -n in one order spends no
  bar flat and pays no partial-fill risk. #2 and #3 failed the floor by tuning a
  trade rate; nothing since has.
* **The band.** At 15 minutes, going narrower is worse: 0.75 gave +0.1269 for 85
  more fills, and the arithmetic on those 85 fills is unambiguous — they were
  worth about 5.2 bp gross each against a 10 bp round trip, so they were paid for
  and not earned. The marginal flip below |z| = 1.0 is a losing trade. #8's 1.0
  sits on the right side of that line, and the prior for moving it either way is
  now weak in both directions.
* **The estimator's shape.** Both decayed variants (#12, #13) *raised* the fill
  count rather than lowering it — 310 and 227 against #8's 167 — and both scored
  worse. A kernel that never forgets is a kernel that re-crosses: the exponen-
  tial tail keeps a slow component alive that the boxcar genuinely discards, and
  the extra crossings it admits are the same class of trade the narrow band
  admitted. Whatever #8's 48-hour window is doing, the sharp exit is part of it,
  not an artefact of it. That question is closed.

That leaves exactly one lever in this family with a measured, monotone, and
theoretically motivated direction, and it is the one #8 opened: **decision lag**.
#4 and #8 hold the same statistic, the same bands, the same state machine, and
the same 48 hours of flow; they differ only in how often the rule is evaluated,
and the finer clock won by +0.031 with six extra fills. The mechanism is not the
signal, it is the wait: a 48-hour ratio moves continuously, but an hourly sampler
only sees it at the top of the hour, so a reversal that crosses `-flip_z` at
09:20 is acted on at 10:00. Every flip therefore carries up to one interval of
avoidable staleness, on a trade whose whole gross edge is about 20 bp of
notional.

#8 took that step from one hour to fifteen minutes and stopped, because that was
the experiment it set out to run. Nobody has taken the second step. This file
does — and it is the same step, not a new one, which is why it is cheap to
interpret.

The single change: the same rule on a five-minute clock
-------------------------------------------------------
Everything in #8 is kept — the rectangular 48-hour flow window, the ratio
`sum(delta)/sum(volume)`, the 30-day dispersion scale, `entry_z = 0.5`,
`flip_z = 1.0`, market orders, one lot, the 15% catastrophe stop, the 24-hour
cooldown — and every parameter that can be expressed in hours is still expressed
in the same number of hours:

    flow_window   576 bars = 48 h      (was 192 bars of 15m)
    sd_window    8640 bars = 30 d      (was 2,880)
    min_sd_obs   2016 bars =  7 d      (was 672)
    cooldown      288 bars = 24 h      (was  96)

Only the sampling of the decision changes: 288 bars a day instead of 96, so the
mean wait after a crossing falls from 7.5 minutes to 2.5. The predicted fill
count is near 167 — a 48-hour average is a slowly varying function of the last 48
hours, so the *sequence* of threshold crossings barely changes; only *when* each
is acted on does, and in one direction only.

Why this is the right step and the last one on this axis
--------------------------------------------------------
The two previous cadence steps tell a consistent story about the size of the
effect. 1h → 15m bought +0.031 for the same trades. The staleness removed by that
step was up to 60 minutes; the staleness removed here is up to 15, so a naive
linear read gives about +0.008 — small, positive, and inside the sampling noise
of 167 flips. I am taking the trial anyway, for two reasons that are worth more
than the point estimate:

* It is the **only** axis on which this family has a measured, signed, mechan-
  ised effect. The band axis is closed by direct arithmetic on marginal fills,
  the estimator-shape axis is closed by two trials pointing the same way, and
  the horizon axis is closed from both ends (6 h gross-negative at #0/#1, 72 h
  at #5 far below 48 h). Chasing a parameter on a closed axis is how a family
  accumulates apparent discoveries that are not there.
* It is **cheap to be wrong about**. If the finer clock admits within-quarter-
  hour crossings that a 15-minute sampler never saw — the same failure mode the
  narrow band had — the fill count is the number that shows it, immediately and
  without interpretation, and the loss is bounded by the fill count times the
  cost of a bad flip. The first thing to read off the run is whether fills moved
  from 167 by a few percent (the lever is lag) or by tens of percent (the lever
  is noise).

Beyond this there is no cadence left to buy: at five minutes the remaining
staleness is 2.5 minutes against a 100 ms order latency and a multi-day hold, and
a one-minute clock would recover only four more minutes while multiplying the
number of chances to cross on nothing.

The rest is #8, unchanged, and deliberately so
---------------------------------------------
* **Measurement.** Over the last 576 bars,

      ratio = sum(bar.delta) / sum(bar.volume)

  which is cumulative volume delta made dimensionless: the fraction of the
  window's aggressive volume that was net buying, minus net selling. Dividing by
  the window's own volume is the campaign's "how to measure large" answered once:
  500 BTC of net buying is a different event in a quiet Asian morning and at a US
  open, and this removes exactly that difference. `unknown_volume` is zero on this
  feed — every trade carries its aggressor — so `bar.delta` is the exact signed
  flow, there is no unknown branch to decide, and no tick subscription is needed:
  ticks cost roughly five times the wall clock for a number the bar already
  carries exactly.

* **"Large" is standardised.** `z = ratio / sd(ratio)` over the trailing 30 days
  of ratio observations, needing 7 days before a scale exists. A fixed cut-off on
  a 24/7 asset is a bet on the volatility regime of the year it was chosen in;
  standardising by the signal's own recent dispersion keeps a band meaning the
  same thing in 2023 and in 2026, which is what the campaign asks for when it
  wants the answer to hold across ordinary sessions. The cut is on the *level* of
  the ratio, not on its deviation from a trailing mean: the claim is that
  one-sided flow predicts, and a month of persistent net buying must not be
  relabelled "not unusual" merely because it happens to be the local average.

* **Hysteresis between entry and flip.** Opening from flat needs |z| > 0.5;
  reversing needs |z| > 1.0. A flip pays two fees and throws away a position the
  flow has not yet contradicted, so it demands a more decisive reversal. The gap
  is what keeps the position on almost always without ever paying to sit out.

* **Market orders, and only on flips.** The fill model is book-queue-pessimistic:
  a resting limit joins behind the displayed size and fills only when trades reach
  through it, which on a continuation entry selects precisely the cases where the
  expected move did not arrive. Paying the touch for a fill that actually happens
  is the cheaper error, and 100 ms of latency against a five-minute decision
  interval is a rounding error on the cadence lever itself.

* **Risk is a backstop, not a rule.** A close-of-bar check at 15% of adverse
  excursion from the average entry, held in code rather than armed as an engine
  order, so no bracket validation, DAY expiry or OCO event can end the run and no
  order id is carried across a callback where an expiry could make it stale. It
  is meant to be crossed by a crash and not by ordinary noise; afterwards the
  strategy stands aside for a day and re-enters on a fresh reading.

* **Both warm-up gates are non-blocking.** A ratio forms once the window is full,
  but an undefined `z` (too few ratio observations for a dispersion estimate) is
  read as maximal in the sign of `ratio` rather than as a reason not to act. The
  first is an estimator warm-up and the second is only the yardstick for "large";
  neither is a market state. On the real split both are long past by the first
  actionable bar of a 960-session run, so this is inert there.

* **Bookkeeping is O(1) per bar and bounded.** The flow window is a deque of 576
  pairs with running sums; the dispersion is running sums over a bounded
  8,640-element deque with a periodic exact re-sync. Nothing rescans history —
  there is no `ctx.history` read at all, so the 512-bar pushed window cannot be
  overrun — and nothing grows across sessions.

Position size is one lot, unchanged. Fees, funding and P&L are linear in size, so
the result is scale-free and nothing is bought by scaling up; and keeping it
identical keeps this trial comparable with the fourteen before it, which is the
only reason a difference of this size can be read at all.

What is deliberately absent: a tick subscription, any fade (the campaign names
the direction), any book-based input (not available on this adapter), a regime
model, a target or bracket, a second instrument, and any fixed-horizon exit. The
horizon *is* the flow window, and the position is held while the flow agrees with
it.

Outcomes and what they would mean
---------------------------------
Above #8 (+0.1677) with a fill count near 167: decision lag was worth more than
the linear read, the cadence axis has one more step in it, and a one-minute clock
is the next trial. Within a few hundredths of #8 either way with a fill count
near 167: the lag lever is exhausted and the family has found its answer, which
is #8's — the campaign's claim survives its costs at a 48-hour horizon and no
further refinement of the clock will change that. A fill count far from 167, in
either direction, with a score below #8: the five-minute clock is manufacturing
crossings that the fifteen-minute clock suppressed, the coarse cadence was
itself acting as a filter, and the honest conclusion is that the family is
complete and the remaining variance is sampling error.
"""

from __future__ import annotations

import math
from collections import deque


class Strategy:
    """CVD continuation: hold with the flow, flip only on a decisive reversal."""

    def __init__(self, interval="5m", flow_window=576, sd_window=8640,
                 min_sd_obs=2016, entry_z=0.5, flip_z=1.0, size=1,
                 stop_bps=1500.0, cooldown_bars=288):
        self.interval = interval            # 5m bars: decision lag cut once more
        self.flow_window = flow_window      # 576 bars = 48 hours of aggressor flow
        self.sd_window = sd_window          # 8640 bars = 30 days of ratio history
        self.min_sd_obs = min_sd_obs        # 2016 bars = 7 days before a scale exists
        self.entry_z = entry_z              # |z| needed to open from flat
        self.flip_z = flip_z                # |z| needed to reverse an open position
        self.size = size
        self.stop_bps = stop_bps            # catastrophe stop, basis points
        self.cooldown_bars = cooldown_bars  # 288 bars = 24 hours flat after a stop

        # Rolling flow window with running sums: (delta, volume) per closed bar.
        self.flow = deque()
        self.sum_delta = 0.0
        self.sum_volume = 0.0

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

        # --- flow window, O(1) ----------------------------------------------
        if len(self.flow) >= self.flow_window:
            old_delta, old_volume = self.flow.popleft()
            self.sum_delta -= old_delta
            self.sum_volume -= old_volume
        delta = float(bar.delta)
        volume = float(bar.volume)
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
        if self._bars % 4096 == 0:
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
            # undefined scale is not a market state.
            if ratio == 0.0:
                return
            z = 2.0 if ratio > 0.0 else -2.0

        # ---- already positioned: hold unless the flow has reversed hard ------
        if self.dir == 1:
            if z < -self.flip_z:
                # One order takes the book from +position to -size: no bar spent
                # flat, and no half-applied flip if only the close had filled.
                if ctx.sell(size=position + self.size,
                            tag="cvd-flip-short") is not None:
                    self.dir = -1
            return
        if self.dir == -1:
            if z > self.flip_z:
                if ctx.buy(size=(-position) + self.size,
                           tag="cvd-flip-long") is not None:
                    self.dir = 1
            return

        # ---- flat: open with the sign of the flow ----------------------------
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
        # Rolling sums of squares drift after tens of thousands of updates;
        # re-deriving them from the bounded deque is cheap and exact.
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
