"""CVD continuation on the BTC perpetual — testing the campaign's claim.

The claim under test: aggressive flow persists. Sum the signed volume over a
window (`bar.delta` = buy_volume - sell_volume, which this dataset always
attributes), and when that imbalance is unusually large, price keeps moving in
the same direction for a while.

Design decisions, and why:

* **Measuring "large".** Raw delta is not comparable across hours and years,
  so the signal is `R = sum(delta over K bars) / sum(volume over K bars)` — a
  signed order-flow imbalance in [-1, 1] that is already scale-free in volume.
  It is then z-scored against its own recent distribution, accumulated on
  `self` (the 512-bar window is too short to hold the statistics I need, so I
  keep a bounded deque of R values and running sums instead of ever touching
  `ctx.history`).

* **Horizon.** 15-minute bars, K = 8 (a two-hour flow window), maximum hold 8
  bars (two hours). The claim is symmetric between measurement and payoff, so
  the horizon you measure over is the horizon you hold. Shorter horizons put
  the 10bp round trip in front of an effect that is only a few bp wide; longer
  ones dilute a signal that decays.

* **Direction.** Long on positive z, short on negative z. Explicitly
  momentum, not a fade — if the claim is wrong this strategy should lose money
  rather than quietly rotate to the opposite side.

* **Exits.** Three, whichever comes first: the hold horizon, a stop at 1.2% of
  entry (a catastrophe stop — roughly eight 15-minute standard deviations, so
  it does not get taken out by ordinary noise), or the signal decaying back to
  |z| < 0.5, which is the thesis "the flow has normalised, the drift is over".

* **Cost discipline.** One position at a time, a four-bar cooldown after any
  exit so a stop-out cannot immediately re-enter the same signal, entries only
  when `ctx.actionable`, and no bracket churn — exits are explicit closes, so
  there is never a stale order id to reason about. At 5bp per side a round
  trip is 0.10%; the filter is deliberately extreme so that trades are few and
  the expected move per trade can plausibly clear that bar.

* **Known limits, stated rather than hidden.** This is one instrument and one
  simple mechanism. It holds through the 00:00/08:00/16:00 UTC funding times
  when a two-hour hold crosses one; funding is charged and is not modelled
  here. BTC volume is not stationary across the 960 scored sessions, which is
  why the normalisation is rolling rather than fixed — but a 1,500-bar rolling
  window still assumes the *shape* of the flow distribution is stable.
"""

from __future__ import annotations

from collections import deque


class Strategy:
    def __init__(
        self,
        interval: str = "15m",
        flow_bars: int = 8,
        stat_bars: int = 1500,
        z_entry: float = 2.0,
        z_exit: float = 0.5,
        max_hold: int = 8,
        cooldown: int = 4,
        stop_pct: float = 0.012,
        size: int = 1,
        min_stat: int = 200,
    ) -> None:
        self.interval = interval
        self.flow_bars = flow_bars
        self.stat_bars = stat_bars
        self.z_entry = z_entry
        self.z_exit = z_exit
        self.max_hold = max_hold
        self.cooldown = cooldown
        self.stop_pct = stop_pct
        self.size = size
        self.min_stat = min_stat

        # Rolling flow window.
        self._deltas: deque = deque(maxlen=flow_bars)
        self._vols: deque = deque(maxlen=flow_bars)

        # Rolling distribution of the imbalance statistic.
        self._stat: deque = deque(maxlen=stat_bars)
        self._sum = 0.0
        self._sumsq = 0.0

        # Position bookkeeping (all values, never the context).
        self._held = 0
        self._cool = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        # Only act on the bar of my own subscription; a coarser or finer
        # subscription closing at the same instant must not drive decisions.
        bar = ctx.bars.get(self.interval)
        if bar is None:
            return

        # --- update the signal, on every closed bar, warmup included --------
        self._deltas.append(bar.delta)
        self._vols.append(bar.volume)
        if len(self._deltas) < self.flow_bars:
            return

        vol = sum(self._vols)
        if vol <= 0:
            return
        r = sum(self._deltas) / vol

        if len(self._stat) == self.stat_bars:
            old = self._stat[0]
            self._sum -= old
            self._sumsq -= old * old
        self._stat.append(r)
        self._sum += r
        self._sumsq += r * r

        z = None
        n = len(self._stat)
        if n >= self.min_stat:
            mean = self._sum / n
            var = self._sumsq / n - mean * mean
            if var > 0.0:
                z = (r - mean) / (var ** 0.5)

        # --- manage an open position --------------------------------------
        if ctx.position != 0:
            self._held += 1
            exit_now = self._held >= self.max_hold

            entry_px = ctx.average_entry_price
            px = bar.close
            if entry_px > 0.0:
                if ctx.position > 0 and px <= entry_px * (1.0 - self.stop_pct):
                    exit_now = True
                elif ctx.position < 0 and px >= entry_px * (1.0 + self.stop_pct):
                    exit_now = True

            if z is not None:
                if ctx.position > 0 and z < self.z_exit:
                    exit_now = True
                elif ctx.position < 0 and z > -self.z_exit:
                    exit_now = True

            if exit_now:
                ctx.close()
                self._held = 0
                self._cool = self.cooldown
            return

        # Flat: respect the post-exit cooldown before considering a new entry.
        if self._cool > 0:
            self._cool -= 1
            return

        # --- entry ---------------------------------------------------------
        if not ctx.actionable:
            return
        if ctx.working_order_ids:
            return
        if z is None or abs(z) < self.z_entry:
            return

        if z > 0.0:
            oid = ctx.buy(size=self.size, tag="cvd-long")
        else:
            oid = ctx.sell(size=self.size, tag="cvd-short")
        if oid is None:
            return                 # dropped by the action cap; no order exists
        self._held = 0

    def on_fill(self, ctx) -> None:
        # Nothing to arm: the exits are bar decisions and `ctx.close()`, so
        # there is no protective order to keep a reference to.
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
