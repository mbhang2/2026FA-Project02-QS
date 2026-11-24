"""CVD continuation on the OKX BTCUSDT perpetual — campaign: "BTC aggressive
flow continues: cumulative volume delta predicts the next move".

The claim under test
--------------------
Aggressive buying that outweighs aggressive selling on the BTC perpetual is
followed by higher prices; aggressive selling by lower ones. The sign of the
signal is the sign of the position. This file follows the flow; it does not fade
it. If continuation is wrong at this horizon the score says so, and rotating to
the other side would answer a different question.

What the prior trials establish
-------------------------------
#0 (1h bars, 6h flow, 6h hold, rolling quantile) made 1,196 round trips and
scored -0.1415: at 10 bp per round trip the fee bill was the whole of the loss,
so its gross edge was about zero. #1 (short hold, wide bracket) was genuinely
negative gross. #2 and #3 cut the trade count and were refused for the exposure
floor (251 and 392 of 960 scored sessions against a required 70%). #4 — a
persistent position held on the sign of a 48-hour flow window, with entry at
|z| > 0.5 and flip at the opposite |z| > 1.0 — traded 161 times and scored
**+0.1365**, the best so far. #5 (72-hour window, median band, 48-bar minimum
hold) scored +0.0201. #6 (12/24/48 consensus, entry 0.4, min hold 24) scored
+0.0864. #7 — #4 with the flip band widened from 1.0 to 1.25 and a 12-bar
minimum hold — traded 127 times and scored +0.0778.

What #7 tells us
----------------
#7's score fell from #4's +0.1365 to +0.0778. The fee difference between 161 and
127 trades is only about 3.4% of notional over the whole split; the score moved
by 5.9 points. So the 34 flips that #7 did not make were, on average, profitable
before fees. Their gross edge was roughly (13.65% + 16.1% fee bill) - (7.78% +
12.7% fee bill) = 9.27% over 34 flips, about 0.273% each, higher than #4's
average 0.185% per flip. The marginal flip was carrying real edge.

The direct consequence is the one #7 named in advance: the band should not
widen further. The next experiment is to narrow it. This file is exactly #4's
estimator and state machine — 48-hour flow window, z-scored against the trailing
30-day dispersion, entry at |z| > 0.5, one lot, market orders, the same 15%
catastrophe stop — with one number changed: the flip threshold falls from 1.0 to
0.75. No minimum holding period, because #7's other change may have contributed
and this trial isolates the band.

Everything else is #4 because #4 is the only configuration with a demonstrated
gross edge and its parts do not need re-testing.

* **Exposure.** A continuously held position is present in every session after
  the first entry, clearing the 70% floor by construction. Flips take +1
  straight to -1 in a single order, so no bar is spent flat between directions.

* **Measurement.** Over the last 48 hourly bars,

      ratio = sum(bar.delta) / sum(bar.volume)

  which is cumulative volume delta made dimensionless: the fraction of the
  window's aggressive volume that was net buying, minus net selling. Dividing by
  the window's own volume is the campaign's "how to measure large" answered
  once — 500 BTC of net buying is a different event in a quiet Asian morning and
  at a US open. `unknown_volume` is zero on this feed, so `bar.delta` is the
  exact signed flow and there is no unknown-aggressor branch to decide. No tick
  subscription: the bar already carries the same number for free, and ticks cost
  roughly five times the wall clock.

* **"Large" is standardised.** `z = ratio / sd(ratio)` over the trailing 720
  hours (30 days), needing 168 observations. A fixed cut-off on a 24/7 asset is
  a bet on the volatility regime of the year it was chosen in; standardising by
  the signal's own recent dispersion keeps a band meaning the same thing in 2023
  and in 2026. The cut is on the *level*, not on the deviation from a trailing
  mean: the claim is about the flow being one-sided, and a month of persistent
  net buying should not be relabelled "not unusual" merely because it is the
  local average.

* **Market orders, and only on flips.** The fill model is book-queue-pessimistic:
  a resting limit joins behind the displayed size and fills only when trades
  reach through it, which on a continuation entry selects precisely the cases
  where the expected move did not arrive. Paying the touch for a fill that
  actually happens is the cheaper error, and 100 ms of latency on a one-lot
  order is not meaningful slippage at hourly scale.

* **Risk is a backstop, not a rule.** A close-of-bar check at 15% of adverse
  excursion from the average entry, held in code rather than armed as an engine
  order, so no bracket validation, DAY expiry or OCO event can end the run and
  no order id is carried across a callback where an expiry could make it stale.
  It is meant to be crossed by a crash and not by ordinary noise; after it fires
  the strategy is flat for a day and re-enters on a fresh reading.

* **Bookkeeping is O(1) per bar.** The flow window is a bounded deque with
  running `delta` and `volume` sums; the standard deviation is running sums over
  a bounded deque with a periodic exact re-sync. Nothing rescans history and
  nothing grows across sessions.

Position size is one lot. Fees, funding and P&L are all linear in it, so the
result is scale-free and the smallest tradable size keeps the trial's risk honest
while the statistics accumulate.

What is deliberately absent: a tick subscription, any fade, any book-based input
(not available on this adapter), a regime model, a target or bracket, a second
instrument, and any fixed-horizon exit. The horizon *is* the flow window, and
the position is held while the flow agrees with it.
"""

from __future__ import annotations

import math
from collections import deque


class Strategy:
    """CVD continuation: hold with the flow, flip on a 0.75-sd reversal."""

    def __init__(self, interval="1h", flow_window=48, sd_window=720,
                 min_sd_obs=168, entry_z=0.5, flip_z=0.75, size=1,
                 stop_bps=1500.0, cooldown_bars=24):
        self.interval = interval
        self.flow_window = flow_window      # hours of aggressor flow in the signal
        self.sd_window = sd_window          # hours of ratio history for the scale
        self.min_sd_obs = min_sd_obs        # observations before the scale exists
        self.entry_z = entry_z              # |z| needed to open from flat
        self.flip_z = flip_z                # |z| needed to reverse a position
        self.size = size
        self.stop_bps = stop_bps            # catastrophe stop, basis points
        self.cooldown_bars = cooldown_bars  # flat hours after a stop-out

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
        history = ctx.history(self.interval)
        if len(history) < self.flow_window:
            return

        window = history.last(self.flow_window)
        total_volume = 0
        total_delta = 0
        for bar in window:
            total_volume += bar.volume
            total_delta += bar.delta
        if total_volume <= 0:
            return
        ratio = total_delta / total_volume

        # The distribution accumulates through warmup as well: those bars are
        # real, only the orders would not be, and reaching the first actionable
        # bar with no idea what "large" means here would waste the warmup.
        self._push_ratio(ratio)
        self._bars += 1
        if self._bars % 512 == 0:
            self._resync()

        if not ctx.actionable:
            return

        if self.cooldown > 0:
            self.cooldown -= 1

        position = ctx.position
        if ctx.working_order_ids:
            return
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
            return

        # ---- already positioned: hold unless the flow has reversed hard ------
        if self.dir == 1:
            if z < -self.flip_z:
                # One order takes +1 to -1: close the long and open the short.
                if ctx.sell(size=2 * self.size, tag="flip-short") is not None:
                    self.dir = -1
            return
        if self.dir == -1:
            if z > self.flip_z:
                if ctx.buy(size=2 * self.size, tag="flip-long") is not None:
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
        # Keep the direction in step with what actually happened.
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
