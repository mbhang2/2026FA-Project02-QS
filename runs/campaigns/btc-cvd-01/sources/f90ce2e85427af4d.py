"""Cumulative volume delta continuation on the BTC perpetual.

The claim under test, as written before the campaign was registered: when
aggressive buying outweighs aggressive selling on the BTC perpetual, price keeps
rising for a while; when aggressive selling outweighs buying, price keeps
falling. Fading large flow is the opposite claim and is not implemented here --
if continuation is wrong, this file should lose money and say so.

How it is built
---------------
* 15-minute bars. The bar already carries `buy_volume` / `sell_volume`, so
  `bar.delta` gives signed flow without a tick subscription. That matters: on
  this engine a tick subscriber is roughly 5x the wall clock for the same
  information at this resolution, and the tick path also loses the engine's
  ability to skip quiet spans.

* The signal is volume-normalised cumulative delta over the last `cvd_bars`
  bars (2 hours at the default):  r = sum(delta) / sum(volume).  Normalising by
  the volume traded in the *same* window is what makes "large" mean the same
  thing at 03:00 and at 14:00, in 2023 and in 2026 -- a fixed contract count
  does not. r is in [-1, 1] and is a pure order-flow imbalance.

* "Unusually large" is a z-score of r against its own trailing distribution,
  which is kept on `self` because `ctx.history(...)` is a bounded 512-bar
  window and the engine silently drops what it drops. The current value is
  excluded from the mean and stdev it is scored against, so one outlier cannot
  move the yardstick it is measured on. `min_abs` is a second gate: if the
  ratio distribution happens to be very tight, a z of 3 can still be an
  economically meaningless imbalance, and we do not want to trade a rounding
  artefact.

* Entry is a market order. A continuation thesis cannot honestly be entered
  with a resting limit: a passive bid fills only when price comes down to it,
  which is precisely the case the signal says will not happen. Paying the
  spread (a fraction of a basis point on this venue) is the correct price of
  admission; the commission (5 bp per side, 10 bp round trip) is the real cost,
  and it is what bounds turnover here.

* Exit is time-based at `hold_bars` -- the horizon is part of the claim, so it
  is stated and held rather than adapted. No profit target: truncating the
  winners is the fastest way to turn a real momentum effect into a negative
  net-of-fees return. A wide protective stop (`stop_frac` of the entry price,
  checked bar by bar against the last trade) exists only as a tail guard and is
  expected to trigger rarely.

* A cooldown of `cooldown_bars` after every exit, plus the z threshold, keeps
  round trips to a handful per day. With 10 bp of commission per round trip,
  turnover control is the single biggest lever on the result; the 64-action cap
  is nowhere near binding.

Horizon held within the strategy: 2 hours to measure, 2 hours to hold. BTC's
2-hour move is on the order of 0.6-1.0% at recent volatility, so a round trip
costs roughly 0.1-0.15 standard deviations of the move being bet on -- large,
which is exactly why the threshold is set high and the hold is not shortened.
"""

from __future__ import annotations

import math
from collections import deque


class Strategy:
    def __init__(
        self,
        interval: str = "15m",
        cvd_bars: int = 8,          # 2 hours of flow at 15m bars
        z_window: int = 288,        # 3 days of r-history, kept on self
        min_samples: int = 96,      # one day before z is trusted
        z_entry: float = 2.0,
        min_abs: float = 0.10,      # imbalance floor, independent of the z
        hold_bars: int = 8,         # 2 hours
        cooldown_bars: int = 4,     # 1 hour of no new risk after an exit
        stop_frac: float = 0.015,   # tail guard only
        size: int = 1,
    ) -> None:
        self.interval = interval
        self.cvd_bars = cvd_bars
        self.z_window = z_window
        self.min_samples = min_samples
        self.z_entry = z_entry
        self.min_abs = min_abs
        self.hold_bars = hold_bars
        self.cooldown_bars = cooldown_bars
        self.stop_frac = stop_frac
        self.size = size

        self.r_hist: deque = deque()
        self.hold_count = 0
        self.cooldown = 0
        self.entry_price = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        recent = ctx.history(self.interval).last(self.cvd_bars)
        if len(recent) < self.cvd_bars:
            return

        volume = 0
        delta = 0
        for bar in recent:
            volume += bar.volume
            delta += bar.delta
        if volume <= 0:
            return
        ratio = delta / volume

        # z of the current imbalance against the trailing distribution, current
        # value excluded. `history` is a window; this deque is what survives it.
        z = None
        if len(self.r_hist) >= self.min_samples:
            n = len(self.r_hist)
            mean = sum(self.r_hist) / n
            var = sum((x - mean) * (x - mean) for x in self.r_hist) / n
            if var > 1e-12:
                z = (ratio - mean) / math.sqrt(var)

        self.r_hist.append(ratio)
        while len(self.r_hist) > self.z_window:
            self.r_hist.popleft()

        # Warmup accumulates the distribution but places no orders.
        if not ctx.actionable:
            return

        if ctx.position != 0:
            self._manage(ctx)
            return

        self.hold_count = 0
        if self.cooldown > 0:
            self.cooldown -= 1
            return
        if ctx.working_order_ids:
            return
        if z is None or abs(ratio) < self.min_abs:
            return

        if z >= self.z_entry:
            ctx.buy(size=self.size, tag="cvd-long")
        elif z <= -self.z_entry:
            ctx.sell(size=self.size, tag="cvd-short")

    def _manage(self, ctx) -> None:
        self.hold_count += 1
        exit_now = self.hold_count >= self.hold_bars

        entry = self.entry_price
        if not exit_now and entry is not None and entry > 0.0:
            price = ctx.last_price
            if ctx.position > 0 and price <= entry * (1.0 - self.stop_frac):
                exit_now = True
            elif ctx.position < 0 and price >= entry * (1.0 + self.stop_frac):
                exit_now = True
        if not exit_now:
            return

        ctx.close()
        self.hold_count = 0
        self.entry_price = None
        self.cooldown = self.cooldown_bars

    def on_fill(self, ctx) -> None:
        if ctx.position != 0:
            self.entry_price = ctx.average_entry_price
        else:
            self.entry_price = None

    def on_lifecycle(self, ctx) -> None:
        pass
