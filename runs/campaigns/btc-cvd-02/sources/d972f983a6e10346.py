"""CVD continuation on the OKX BTCUSDT perpetual — campaign: "BTC aggressive
flow continues: cumulative volume delta predicts the next move".

The claim under test
--------------------
Aggressive buying that outweighs aggressive selling on the BTC perpetual is
followed by higher prices, and aggressive selling by lower ones. The sign of the
accumulated flow is the sign of the position. This file follows the flow; it does
not fade it.

The record, and what it has settled
-----------------------------------
#0 (1h bars, 6h flow, 6h hold, rolling quantile) made 1,196 round trips and
scored -0.1415: at 10 bp a round trip the fee bill was the whole of the loss, so
its gross edge was about zero. #1 (short hold, wide bracket) was genuinely
negative gross. #2 and #3 cut the trade count and were refused for the exposure
floor (251 and 392 of 960 scored sessions against a required 70%). #4 — a
persistent position held on the sign of a 48-hour flow window, z-scored against
its own trailing 30-day dispersion, entry at |z| > 0.5, flip at the opposite
|z| > 1.0 — traded 161 times and scored **+0.1365**. #5 (72-hour window) +0.0201.
#6 (12/24/48 consensus) +0.0864 with 264 fills. #7 (#4 with flip_z 1.25 and a
12-bar minimum hold) +0.0778 with 127 fills. #8 (#4 on 15-minute bars, same 48
hours of flow, same bands) **+0.1677** with 167 fills. #9 (#4 with flip_z 0.75,
hourly) +0.1545 with 221 fills. #10/#11 (15m with flip_z 0.75) +0.1269 with 252
fills — the second submission of that file was a no-op edit and reproduced the
first byte for byte, which is itself a fact about how much of this family is
parameter-insensitive.

Read together, three things are settled and I will not spend trials on them
again:

* **The shape.** A continuously held long-or-short clears the exposure floor by
  construction, and a flip that takes +n straight to -n in one order spends no
  bar flat and pays no partial-fill risk. #2 and #3 failed the floor by tuning a
  trade rate; nothing here does.
* **The cadence.** 15 minutes beat an hour at constant bands (+0.1365 →
  +0.1677) while adding six fills. The 48-hour ratio is smooth, so the finer
  clock did not change *which* crossings occurred; it changed *when* they were
  acted on, and earlier was better. That is a statement about decision lag, and
  it is worth about three points of score on its own.
* **The band.** Monotone at hourly (1.25 → 1.0 → 0.75 gave +0.0778 → +0.1365 →
  +0.1545), and *non-additive* with the finer clock: at 15 minutes, 0.75 gave
  +0.1269 against 1.0's +0.1677. The extra 85 flips at the narrow band cost
  ~0.043 in fees and returned ~0.001 gross — they were noise. So cadence and
  band are substitutes, not complements, and the surface has one effective
  dimension, not two.

What is left is the estimator, and this trial changes exactly one thing about it
------------------------------------------------------------------------------
Everything tested so far — 6h, 48h, 72h, single window and three-window
consensus — differs either in how long the window is or in how often it is
sampled. None of them differs in **shape**. Every one of them is a boxcar: 192
equal weights and then a cliff. A bar contributes fully for 48 hours and then,
on the next close, contributes nothing at all. The ratio therefore jumps on
every bar's *expiry* as well as on its arrival, and those jumps are pure
estimator artefact — they are the same size as the jumps caused by news, they
are uncorrelated with anything the market is doing, and every one of them can
cross a band.

So this file keeps #8's cadence (15m), #8's bands (entry 0.5, flip 1.0), #8's
state machine, #8's standardisation (level of the ratio over its trailing 30-day
sd, not deviation from a trailing mean) and replaces the boxcar with an
**exponential kernel**:

    ew_delta  = a * ew_delta  + bar.delta
    ew_volume = a * ew_volume + bar.volume
    ratio     = ew_delta / ew_volume,     a = 0.5 ** (1 / halflife_bars)

with `halflife_bars = 64` (16 hours). The half-life is chosen to hold the
horizon fixed rather than to change it: the mean age of the data in a uniform
192-bar window is 96 bars, and an exponential kernel with half-life h has mean
age h/ln2, so h = 96 * ln2 ≈ 66 and 64 is the round number. This is therefore
the *same* horizon with a different weighting, not a shorter window wearing a
new name — the freshest bar carries 1/96 of the weight instead of 1/192, the
tail decays instead of vanishing, and no bar ever leaves.

Why this should help, and why it might not
------------------------------------------
For it: both measured improvements in this family came from acting on the same
information sooner (#8) or from discriminating between crossings more sharply
(#9, and its failure at 15m). A kernel that decays removes the expiry
discontinuity, which is a source of crossings that carry no information — the
same class of event that made the narrow band worthless at the finer clock. If
part of #8's remaining cost is flips triggered by a bar rolling out of the
window, this reduces it without touching the threshold that governs genuine
reversals.

Against it: the boxcar's abruptness may be doing useful work. A hard window
genuinely forgets a day-old burst of buying, and an exponential tail never
does; if flow's predictive power really does end at some horizon, the tail is
dilution. And smoothing a series whose sd is estimated from the same series
co-moves both numerator and scale, so |z| may drift as a level — which shows up
as a fill count far from 167, and that count is the first thing to read.

The rest is #8, unchanged, and deliberately so
----------------------------------------------
* **Measurement.** `ratio = sum(bar.delta) / sum(bar.volume)` over the decayed
  kernel: cumulative volume delta made dimensionless, the fraction of the
  window's aggressive volume that was net buying. Dividing by the window's own
  volume is the campaign's "how to measure large" answered once — 500 BTC of net
  buying is a different event in a quiet Asian morning and at a US open.
  `unknown_volume` is zero on this feed, so `bar.delta` is exact signed flow and
  no tick subscription is needed: ticks cost roughly five times the wall clock
  for a number the bar already carries exactly.

* **"Large" is standardised.** `z = ratio / sd(ratio)` over the trailing 30 days,
  needing 7 days of observations. A fixed cut-off on a 24/7 asset is a bet on the
  volatility regime of the year it was chosen in. The cut is on the *level* of
  the ratio, not its deviation from a trailing mean: the claim is that one-sided
  flow predicts, and a month of persistent net buying should not be relabelled
  "not unusual" merely because it is the local average.

* **Hysteresis between entry and flip.** Opening from flat needs |z| > 0.5;
  reversing needs |z| > 1.0. A flip pays two fees and throws away a position the
  flow has not yet contradicted. The gap is what keeps the position on almost
  always without ever paying to sit out, which is also what clears the 70%
  exposure floor by construction rather than by tuning a trade rate.

* **Market orders, and only on flips.** The fill model is book-queue-pessimistic:
  a resting limit joins behind the displayed size and fills only when trades
  reach through it, which on a continuation entry selects precisely the cases
  where the expected move did not arrive. Paying the touch for a fill that
  actually happens is the cheaper error, and 100 ms against a 15-minute decision
  interval is a rounding error on the cadence lever itself.

* **Risk is a backstop, not a rule.** A close-of-bar check at 15% of adverse
  excursion from the average entry, held in code rather than armed as an engine
  order, so no bracket validation, DAY expiry or OCO event can end the run and no
  order id is carried across a callback where an expiry could make it stale. It
  is meant to be crossed by a crash and not by ordinary noise; afterwards the
  strategy stands aside for a day and re-enters on a fresh reading.

* **Neither warm-up gate blocks.** The ratio forms after `min_flow_bars` bars
  rather than only on a full window, and an undefined z (too few ratio
  observations for a dispersion estimate) is read as maximal in the sign of the
  ratio. Neither gate is a market state — the first is an estimator warm-up and
  the second is only the yardstick for "large" — and gating on them silently is
  how a previous draft of this family made no trades at all on a short walk. On
  the real split both are long past by the first actionable bar, so this is inert
  there.

* **Bookkeeping is O(1) per bar.** The kernel is two scalars; the dispersion is
  running sums over a bounded 2,880-element deque with a periodic exact re-sync.
  Nothing rescans history — there is no `ctx.history` read at all, so the 512-bar
  pushed window cannot be overrun — and nothing grows across sessions.

Position size is one lot. The score is a return on notional; fees, funding and
P&L are all linear in size, so the result is scale-free and nothing is bought by
scaling up. What is deliberately absent: a tick subscription, any fade (the
campaign names the direction), any book-based input (not available on this
adapter), a regime model, a target or bracket, a second instrument, and any
fixed-horizon exit. The horizon *is* the flow kernel, and the position is held
while the flow agrees with it.

Outcomes and what they would mean
---------------------------------
Above #8 (+0.1677) with a fill count near 167: the boxcar's expiry cliff was a
real source of false flips, the estimator is the axis that still has room, and
the next step is the same kernel once more — a longer half-life inside the same
mean age, or a second, slower kernel for the scale. Within noise of #8: the
kernel shape is not a lever here, the family has converged on its signal, and the
honest conclusion is that the campaign's answer is the one #8 already gave.
Materially below #8, or a fill count far from 167: the tail is dilution and the
hard window was doing the work — which would say the flow's predictive power ends
sharply at about 48 hours, and that is the most useful thing left to learn.
"""

from __future__ import annotations

import math
from collections import deque


class Strategy:
    """CVD continuation: hold with the flow, flip on a decisive reversal."""

    def __init__(self, interval="15m", halflife_bars=64, sd_window=2880,
                 min_sd_obs=672, entry_z=0.5, flip_z=1.0, size=1,
                 stop_bps=1500.0, cooldown_bars=96, min_flow_bars=4):
        self.interval = interval            # 15m bars: #8's clock
        self.halflife_bars = halflife_bars  # 64 bars = 16h; mean data age = 96 bars
        self.sd_window = sd_window          # 2880 bars = 30 days of ratio history
        self.min_sd_obs = min_sd_obs        # 672 bars = 7 days before a scale exists
        self.entry_z = entry_z              # |z| needed to open from flat
        self.flip_z = flip_z                # |z| needed to reverse an open position
        self.size = size
        self.stop_bps = stop_bps            # catastrophe stop, basis points
        self.cooldown_bars = cooldown_bars  # 96 bars = 24 hours flat after a stop
        self.min_flow_bars = min_flow_bars  # bars before any ratio exists at all

        # The decay factor. 0.5 ** (1/h) halves a bar's weight every h bars.
        if halflife_bars <= 0:
            self.alpha = 0.5
        else:
            self.alpha = 0.5 ** (1.0 / float(halflife_bars))

        # Exponentially weighted flow: two scalars, no window to expire.
        self.ew_delta = 0.0
        self.ew_volume = 0.0
        self._flow_bars = 0

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

        # --- exponentially weighted flow, O(1) ------------------------------
        a = self.alpha
        self.ew_delta = a * self.ew_delta + float(bar.delta)
        self.ew_volume = a * self.ew_volume + float(bar.volume)
        self._flow_bars += 1

        # A full kernel is the signal; a partial one at the very start of a run
        # is still a reading of the same statistic, and refusing to form it is
        # not a market state -- it is an estimator warm-up.
        if self._flow_bars < self.min_flow_bars or self.ew_volume <= 0.0:
            return
        ratio = self.ew_delta / self.ew_volume

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
            # what is missing is only the yardstick for "large". With no
            # yardstick the sign of the flow is the whole of the evidence, so
            # read it as maximal in that direction rather than declining to act
            # -- an undefined scale is not a market state.
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
