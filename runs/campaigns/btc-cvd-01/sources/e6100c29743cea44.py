"""Cumulative volume delta continuation on the OKX BTC perpetual.

Claim under test (campaign brief, 2026-11-22): unusually large *positive*
cumulative volume delta is followed by higher prices over the next comparable
window, and unusually large *negative* CVD by lower prices. This is the
continuation side of that claim, not the fade.

Construction
------------
* **5-minute bars.** Every bar carries `buy_volume` / `sell_volume`, so
  `bar.delta` is signed aggressive flow without subscribing to ticks. That
  matters here: a tick subscription on this data multiplies the work by
  hundreds and buys nothing at a multi-hour horizon.

* **Signal.**  r_t = sum(delta over the last `flow_window` bars) /
  sum(volume over the same bars) -- the aggressive-flow imbalance over the
  last hour, in [-1, 1]. A *ratio* rather than a contract count, because BTC
  volume differs by an order of magnitude across hours and years and a fixed
  size would mean different things at different times. This is the "normalise
  against something the data already seen supplies" the brief asks for.

* **"Large".** A z-score of r_t against the distribution of the same statistic
  over the preceding `z_lookback` bars (2,016 five-minute bars = seven days).
  Rolling rather than cumulative so the threshold tracks regime drift; seven
  days is long enough to estimate a scale and short enough not to be dominated
  by last year's volatility. Six-sevenths of that window is warmup before the
  first trade is possible, which is fine -- the walk serves warmup sessions
  and this strategy simply accumulates through them.

* **Entry.** z >= +3 buys, z <= -3 sells, market order, one position at a time.
  Deliberately selective: at 5 bp per side a round trip must clear 10 bp before
  it earns anything, so the strategy waits for flow that is extreme relative to
  its own recent history rather than trading every imbalance. The width of the
  threshold is the main defence against the cost line.

* **Exit.** The claim names a horizon, so the exit is the horizon: hold
  `hold_bars` (two hours) and then close at market, unconditionally. A 2%
  adverse move from the average entry is a tail guard only -- roughly 2.5 sigma
  of a two-hour BTC move -- there to cap a liquidation cascade, not to shape
  the return distribution. No profit target: a target would truncate exactly
  the continuation the claim is about.

* **Size.** 100 lots = 0.01 BTC. A market order must be fillable inside the top
  five book levels; 0.01 BTC is far inside normal OKX top-of-book depth, so the
  order is never refused for size, and the P&L per trade is small relative to
  the account in the same proportion the costs are.

Why market orders and not resting limits: the fill model here is
book-queue-pessimistic -- a resting limit fills only when a trade goes strictly
through it. A limit entry is therefore adversely selected (it fills when the
flow has turned against you) and systematically fails to fill on the strongest
continuation signals, which is precisely the sample the claim is about. Paying
the spread with a market order is the honest way to test a directional claim,
and at 10 bp of round-trip cost the spread is not the dominant term.

Nothing here recomputes history: the rolling z-score keeps a bounded deque with
running sums, and the signal window is a `last(k)` read inside the 512-bar
window the harness pushes.
"""

from __future__ import annotations

from collections import deque


class Strategy:
    """Buy extreme aggressive buying, sell extreme aggressive selling."""

    def __init__(
        self,
        interval: str = "5m",
        flow_window: int = 12,
        hold_bars: int = 24,
        z_entry: float = 3.0,
        z_lookback: int = 2016,
        min_history: int = 200,
        size: int = 100,
        stop_frac: float = 0.02,
    ) -> None:
        self.interval = interval
        self.flow_window = int(flow_window)
        self.hold_bars = int(hold_bars)
        self.z_entry = float(z_entry)
        self.z_lookback = int(z_lookback)
        self.min_history = int(min_history)
        self.size = int(size)
        self.stop_frac = float(stop_frac)

        # Rolling distribution of the flow-ratio statistic. Bounded: the
        # oldest value is subtracted as it falls off, so nothing grows.
        self.ratios: deque = deque(maxlen=self.z_lookback)
        self.r_sum = 0.0
        self.r_sumsq = 0.0

        self.bars_held = 0

    # -- statistics -----------------------------------------------------

    def _push(self, value: float) -> None:
        if len(self.ratios) == self.ratios.maxlen:
            old = self.ratios[0]
            self.r_sum -= old
            self.r_sumsq -= old * old
        self.ratios.append(value)
        self.r_sum += value
        self.r_sumsq += value * value

    def _stats(self):
        n = len(self.ratios)
        if n < self.min_history:
            return None, None
        mean = self.r_sum / n
        var = self.r_sumsq / n - mean * mean
        if var <= 0.0:
            return None, None
        sd = var ** 0.5
        if sd <= 1e-9:
            return None, None
        return mean, sd

    # -- callbacks ------------------------------------------------------

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        recent = ctx.history(self.interval).last(self.flow_window)
        if len(recent) < self.flow_window:
            return

        volume = 0
        delta = 0
        for bar in recent:
            volume += bar.volume
            delta += bar.delta
        if volume <= 0:
            return

        ratio = delta / float(volume)
        # Accumulate on every bar, warmup included: the z-score needs a scale
        # and the warmup sessions are exactly the place to estimate it.
        self._push(ratio)

        if not ctx.actionable:
            return

        if ctx.position != 0:
            self._manage(ctx)
            return
        if ctx.working_order_ids:
            return

        mean, sd = self._stats()
        if mean is None:
            return
        z = (ratio - mean) / sd

        if z >= self.z_entry:
            ctx.buy(size=self.size, tag="cvd-long")
        elif z <= -self.z_entry:
            ctx.sell(size=self.size, tag="cvd-short")

    def _manage(self, ctx) -> None:
        # An exit is already in flight; wait for it rather than stacking.
        if ctx.working_order_ids:
            return

        self.bars_held += 1

        if self.bars_held >= self.hold_bars:
            ctx.close()
            return

        entry = ctx.average_entry_price
        if entry <= 0.0:
            return
        last = ctx.last_price
        if ctx.position > 0:
            if last <= entry * (1.0 - self.stop_frac):
                ctx.close()
        elif ctx.position < 0:
            if last >= entry * (1.0 + self.stop_frac):
                ctx.close()

    def on_fill(self, ctx) -> None:
        # A new position starts its clock here; a flat book clears it. Position
        # size after the fill is the only thing that distinguishes the two.
        self.bars_held = 0

    def on_lifecycle(self, ctx) -> None:
        pass
