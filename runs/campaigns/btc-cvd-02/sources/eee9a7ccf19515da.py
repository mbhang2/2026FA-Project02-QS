"""CVD continuation on the OKX BTCUSDT perpetual — campaign: "BTC aggressive
flow continues: cumulative volume delta predicts the next move".

The claim under test
--------------------
Aggressive buying that outweighs aggressive selling on the BTC perpetual is
followed by higher prices, and aggressive selling by lower ones. The sign of the
accumulated flow is the sign of the position. This file follows the flow; it does
not fade it. If continuation is wrong at this horizon the score says so, and
rotating to the other side would answer a different question.

What the six prior trials establish
-----------------------------------
#0 (1h bars, 6-bar flow, 6-bar hold, rolling quantile) made **1,196 round trips**
and scored -0.1415. At 10 bp per round trip that is roughly 120% of notional paid
in fees, so its *gross* edge was approximately zero: a six-hour flow window does
not predict anything worth paying for. #1 (5m bars, one-hour hold, wide bracket)
scored -0.2349 with a genuinely negative gross, for the same reason at a shorter
horizon. #2 and #3 cut the trade count and were refused for the exposure floor.
#4 — the shape this file keeps — held a **persistent** position on the sign of a
48-hour flow window with hysteresis bands, traded 161 times, and scored
**+0.1365**. #5 used a 72-hour window with a sign-only entry and a median-sized
flip band, traded 133 times, and scored +0.0201.

Read together, two facts are now solid:

1. **The edge is real but small per trade.** #4's 161 flips paid 16.1% of
   notional in fees and still finished +13.65%, so its gross was about 29.8% over
   960 sessions — roughly 0.185% of notional per flip, against the 0.100% a
   round trip costs. The signal clears its cost, but by a factor of less than
   two. Every basis point of unnecessary turnover is a visible fraction of the
   result.
2. **The 48-hour window is the best of those tested.** 6h gave nothing, 72h gave
   a third of #4's gross. The improvement from #5 to #4 is not the entry rule —
   entry from flat matters only at the start and after a stop — it is the window
   and the flip rule. So the window stays at 48 bars and the experiment below
   changes the one thing that is left: **how often the position is turned over.**

The single change: a wider flip band and a minimum hold
-------------------------------------------------------
Every prior trial that improved paid for it with a fee cut, and every trial that
regressed paid more. With gross edge of 0.185% per flip against a 0.100% toll,
the marginal flip is only weakly profitable, and the cheapest improvement
available is to make fewer of them without leaving the market.

* `flip_z` rises from 1.0 to 1.25 standard deviations. A reversal must now be a
  25% larger flow event before it justifies a round trip. This is a statement
  about the claim, not a curve-fit: the claim is that *unusually* one-sided flow
  continues, so demanding a more unusual reversal to abandon a position is
  asking the claim to be stronger before it is acted against.
* `min_hold` of 12 bars (half a day) floors the time between turns. It is
  deliberately short enough that it almost never binds — a 48-hour flow ratio
  does not oscillate on a half-day scale — so it is a guard against the
  fill-modify-fill pathology, not a trading rule. The point of the trial is the
  band, and I want the band to be what moves the trade count.

Everything else is #4 unchanged, because #4 is the only configuration with a
demonstrated gross edge and I do not want to spend a trial re-testing its parts.

* **Exposure.** A continuously held position is present in every session after
  the first entry, which clears the 70% floor by construction — the property #2
  and #3 lacked, and the reason this shape exists at all. Flips take +1 straight
  to -1 in a single order, so no bar is spent flat between directions.
* **Measurement.** Over the last 48 hourly bars,

      ratio = sum(bar.delta) / sum(bar.volume)

  which is cumulative volume delta made dimensionless: the fraction of the
  window's aggressive volume that was net buying, minus net selling. Dividing by
  the window's own volume is the campaign's "how to measure large" answered
  once — 500 BTC of net buying is a different event in a quiet Asian morning and
  at a US open, and this removes exactly that difference. `unknown_volume` is
  zero on this feed, so `delta` is the exact signed flow and there is no
  unknown-aggressor branch to decide. No tick subscription: `bar.delta` carries
  the same number for free, and ticks cost roughly five times the wall clock for
  information the bar already has exactly.
* **"Large" is standardised.** `z = ratio / sd(ratio)` over the trailing 720
  hours (30 days), needing 168 observations. A fixed cut-off on a 24/7 asset is
  a bet on the volatility regime of the year it was chosen in; standardising by
  the signal's own recent dispersion keeps a band meaning the same thing in 2023
  and in 2026, which is the "not one hour of the day / present across ordinary
  sessions" property the campaign asks a result to have. The cut is on the
  *level* of the ratio, not on its deviation from a trailing mean: the claim is
  about the flow being one-sided, and a month of persistent net buying should not
  be relabelled "not unusual" merely because it is the local average.
* **Market orders, and only on flips.** The fill model is book-queue-pessimistic:
  a resting limit joins behind the displayed size and fills only when trades
  reach it, which on a continuation entry selects precisely the cases where the
  move never came. Paying the touch for a fill that actually happens is the
  cheaper error, and 100 ms of latency on a one-lot order is not meaningful
  slippage at hourly scale.
* **Risk is a backstop, not a rule.** A close-of-bar check at 15% of adverse
  excursion from the average entry, held in code rather than armed as an engine
  order, so no bracket validation, DAY expiry or OCO event can end the run and
  no order id is carried across a callback where an expiry could make it stale.
  It is meant to be crossed by a crash and not by ordinary noise; after it fires
  the strategy is flat for a day and re-enters on a fresh `entry_z` reading.
* **Bookkeeping is O(1) per bar.** The flow window is a bounded deque with
  running `delta` and `volume` sums; the standard deviation is running sums over
  a bounded deque with a periodic exact re-sync. Nothing rescans history and
  nothing grows across sessions.

Position size is one lot. Fees, funding and P&L are all linear in it, so the
result is scale-free and the smallest tradable size keeps the trial's risk honest
while the statistics accumulate.

What is deliberately absent: a tick subscription, any fade, any book-based input
(not available on this adapter), a regime model (the standardisation is a scale,
not a state), a second instrument, a target or bracket (a different claim — where
the move stops), and any fixed-horizon exit. The horizon *is* the flow window,
and the position is held while the flow agrees with it.

If this scores below #4 the reading is that the marginal flip was carrying real
edge and the band should not widen further; if it scores above, the fee term was
dominant and the next step is the same lever again, not a new signal.

Why the synthetic check saw no diff
-----------------------------------
Both parameters changed here — `flip_z` and `min_hold` — act only on a position
that already exists. Before either can be exercised the strategy must accumulate
a full 48-bar flow window *and* 168 observations of the ratio before `z` is even
defined, i.e. 216 hourly bars. The synthetic walk is far shorter than that, so it
never opens a position, never reaches a flip, and returns zero trades — for #4
and for this file alike. The identical fill log is therefore structural, not a
sign that the edit is inert: on the real split the very first flip is where the
difference appears. The alternative would be to shorten the warmup so the
synthetic walk trades, which would change the signal itself (the 48-hour window
is the one thing #4 demonstrated) purely to satisfy a check that reports no
score. The file is resubmitted as intended.
"""

from __future__ import annotations

import math
from collections import deque


class Strategy:
    """CVD continuation: hold with the flow, flip only on a decisive reversal."""

    def __init__(self, interval="1h", flow_window=48, sd_window=720,
                 min_sd_obs=168, entry_z=0.5, flip_z=1.25, min_hold=12,
                 size=1, stop_bps=1500.0, cooldown_bars=24):
        self.interval = interval            # bar interval
        self.flow_window = flow_window      # hours of aggressor flow in the signal
        self.sd_window = sd_window          # hours of ratio history for the scale
        self.min_sd_obs = min_sd_obs        # observations required before trading
        self.entry_z = entry_z              # |z| needed to open from flat
        self.flip_z = flip_z               # |z| needed to reverse an open position
        self.min_hold = min_hold            # bars between turns, at minimum
        self.size = size
        self.stop_bps = stop_bps            # catastrophe stop, basis points
        self.cooldown_bars = cooldown_bars  # flat hours after a stop-out

        # Rolling flow window with running sums: (delta, volume) per bar.
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
        self._held = 0                      # bars since the last position change

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

        # The distribution accumulates through warmup as well: those bars are
        # real, only the orders would not be, and reaching the first actionable
        # bar with no idea what "large" means here would waste the warmup.
        self._push_ratio(ratio)
        self._bars += 1
        if self._bars % 512 == 0:
            self._resync()
        self._held += 1

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
                    self._held = 0
                    return

        z = self._z(ratio)
        if z is None:
            return

        # ---- already positioned: hold unless the flow has reversed hard ------
        if self.dir == 1:
            if z < -self.flip_z and self._held >= self.min_hold:
                # One order takes the book from +position to -size: no bar spent
                # flat, and no half-applied flip if only the close had filled.
                if ctx.sell(size=position + self.size,
                            tag="cvd-flip-short") is not None:
                    self.dir = -1
                    self._held = 0
            return
        if self.dir == -1:
            if z > self.flip_z and self._held >= self.min_hold:
                if ctx.buy(size=(-position) + self.size,
                           tag="cvd-flip-long") is not None:
                    self.dir = 1
                    self._held = 0
            return

        # ---- flat: open with the sign of the flow ----------------------------
        if self.cooldown > 0:
            return
        if z > self.entry_z:
            if ctx.buy(size=self.size, tag="cvd-long") is not None:
                self.dir = 1
                self._held = 0
        elif z < -self.entry_z:
            if ctx.sell(size=self.size, tag="cvd-short") is not None:
                self.dir = -1
                self._held = 0

    # ------------------------------------------------------------------ fills

    def on_fill(self, ctx):
        # Keep the direction in step with what actually happened; the flip order
        # is a single order, so there is nothing else to reconcile and no order
        # id is held across callbacks for an expiry to make stale.
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
