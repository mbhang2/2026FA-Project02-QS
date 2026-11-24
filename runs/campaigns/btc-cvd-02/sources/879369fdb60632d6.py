"""CVD continuation on the OKX BTCUSDT perpetual — campaign: "BTC aggressive
flow continues: cumulative volume delta predicts the next move".

The claim under test
--------------------
Aggressive buying that outweighs aggressive selling on the BTC perpetual is
followed by higher prices; aggressive selling by lower ones. The sign of the
signal is the sign of the position. This file does not fade. If continuation is
wrong at this horizon the score says so; rotating to the other side would test a
different hypothesis and answer nothing.

What the four prior trials established
--------------------------------------
#0 (1h bars, 6h flow window, 6h hold, 0.85 rolling quantile) made **1,196 round
trips over 960 sessions** and scored -0.1415. At 10bp a round trip that is
roughly 60 units of notional paid in fees alone, and it accounts for essentially
all of the loss: the gross edge was approximately zero, not negative.
#1 (short hold, wide two-sided bracket) lost more than its fees could explain —
its per-trade gross was genuinely bad, because a 2% bracket on a one-hour hold
is touched by noise on the stop and not on the target.
#2 and #3 cut the trade count hard (0.98 quantile, then a 24-bar cooldown) and
were **refused for the exposure floor**: 251 and 392 of 960 scored sessions,
against a registered floor of 70%.

So there are two binding constraints, and they pull in opposite directions:

  * the fee is a fixed 10bp per round trip while the opportunity grows with the
    square root of the holding period, so **turnover** is what kills the
    strategy; and
  * the position must exist in **at least 70% of scored sessions**, so going
    quiet is not available either.

Trial #0 satisfied the second and was eaten by the first. #2 and #3 satisfied
the first and were refused for the second. The design that satisfies both at
once is not "trade rarely" — it is **hold continuously and trade rarely**.

The design that follows
-----------------------
A persistent target position. Once the strategy owns a direction it keeps it,
across sessions, across funding times, until the flow reverses decisively
enough to justify paying for a flip. There is no horizon exit and no target:
the position is re-evaluated every bar and only acted on when the evidence
points the other way.

* **Exposure.** A continuously held long or short is a position in every session
  after the first entry, which clears the 70% floor by construction rather than
  by tuning a trade rate. That is the property #2 and #3 lacked.

* **Turnover.** A flip is the only trade, and it costs one round trip (a
  two-lot market order that closes one lot and opens one). With roughly one flip
  every one to three weeks across 960 sessions, total fee drag is a few percent
  of notional over the whole split — against sixty for trial #0. Whatever the
  flow signal is worth, it is no longer paid for many times over.

* **Measurement.** Over the last `flow_window` hourly bars,

      ratio = sum(bar.delta) / sum(bar.volume)

  which lies in [-1, +1] and reads as "the fraction of the window's aggressive
  volume that was net buying, minus net selling". That is the campaign's "how to
  measure large" answered once: 500 BTC of net buying is a different event in a
  quiet Asian morning and a US open, and dividing by the same window's own
  volume removes exactly that difference. `bar.delta` is buy_volume minus
  sell_volume, and `unknown_volume` is zero on this feed, so no unknown-aggressor
  branch has anything to decide — and no tick subscription is needed, which is
  roughly five times the wall clock for information the bar already carries
  exactly.

* **"Large" is standardised, not fixed.** The entry and flip bands are in units
  of the trailing 30-day standard deviation of the ratio itself:

      z = ratio / sd(ratio over the last `sd_window` bars)

  A fixed cut-off on a 24/7 asset is a bet on the volatility regime of the year
  it was chosen in. Standardising by the signal's own recent dispersion keeps
  the bands meaningful in 2023 and in 2026, which is what the campaign asks for
  when it wants the answer to hold across ordinary sessions rather than one
  regime. The cut is on the *level* of the ratio, not on its deviation from a
  trailing mean: the claim is about the flow itself being one-sided, so a
  month of persistent net buying should not be re-labelled "not unusual" just
  because it is the local average.

* **A deliberate asymmetry between entry and flip.** Starting from flat (the
  first actionable bar, or after a catastrophe stop) needs |z| > `entry_z` — a
  moderately one-sided window is enough. Reversing an existing position needs
  |z| > `flip_z`, which is stricter, because a flip pays two fees and throws
  away a position the flow has not yet contradicted. The gap between them is the
  hysteresis that keeps the trade count down without ever taking the strategy
  out of the market.

* **Market orders, and only on flips.** The fill model is queue-pessimistic: a
  resting limit joins behind the displayed size and fills only when trades
  reach it. On a continuation entry that selects exactly the cases where the
  expected move did not arrive — the limits that fill are the ones the flow was
  wrong about — so paying the touch for a fill that actually happens is the
  cheaper error. One lot is 0.0001 BTC; 100 ms of latency on that at hourly
  scale is not a meaningful slippage assumption.

* **Risk is a backstop, not a rule.** A close-of-bar check at 15% of adverse
  excursion from the average entry, held in code rather than armed as an engine
  order, so no bracket validation, DAY expiry or OCO event can end the run and
  no order id is carried across a callback where an expiry could make it stale.
  It is meant to be crossed by a crash and not by ordinary noise; after it fires
  the strategy goes flat for a day and re-enters on a fresh `entry_z` reading,
  so a stopped-out direction is not re-bought the next hour.

* **Bookkeeping is O(1) per bar.** Sums over the flow window come from
  `history.last(k)` on a 48-bar window, and the standard deviation is maintained
  as running sums over a bounded deque with a periodic exact re-sync. Nothing
  rescans the history. One subscription, one lookup, no ticks.

Position size is one lot. Fees, funding and P&L are all linear in it, so the
result is scale-free and the smallest tradable size keeps the trial's risk
honest while the statistics accumulate.

What is deliberately absent: a tick subscription (five times the wall clock for
information `bar.delta` already carries), any fade, any book-based input (not
available on this adapter), a regime model (the standardisation is a scale, not
a state), a second instrument, and any fixed-horizon exit — the horizon *is* the
flow window, and the position is held while the flow agrees with it.
"""

from __future__ import annotations

import math
from collections import deque


class Strategy:
    """CVD continuation: hold with the flow, flip only when it reverses hard."""

    def __init__(self, interval="1h", flow_window=48, sd_window=720,
                 min_sd_obs=168, entry_z=0.5, flip_z=1.0, size=1,
                 stop_bps=1500.0, cooldown_bars=24):
        self.interval = interval
        self.flow_window = flow_window      # hours of aggressor flow in the signal
        self.sd_window = sd_window          # hours of ratio history for the scale
        self.min_sd_obs = min_sd_obs        # observations required before trading
        self.entry_z = entry_z              # |z| needed to open from flat
        self.flip_z = flip_z                # |z| needed to reverse an open position
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
