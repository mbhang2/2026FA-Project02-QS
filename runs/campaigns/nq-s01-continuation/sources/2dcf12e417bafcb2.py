"""Does an outsized move inside a session continue, or give it back?

Ninth proposal. Prior results:
- #1 session-range percentile continuation: +0.0874
- #2 give-back inversion of #1: +0.0047
- #4 rolling-window continuation: +0.0836
- #5 breakout-confirmed continuation: -0.0092
- #6 adaptive percentile continuation: -0.0196
- #7 two-bar confirmation: +0.4534

#7 was a big jump. It combined two-bar confirmation with a session-range
percentile threshold and a fixed bracket. The improvement came from requiring
the outsized bar to be confirmed by a second bar in the same direction, which
filters one-bar spikes.

This version keeps the confirmed-continuation core but adds three refinements
to improve selectivity and capture more of the continuation move:

1. **Larger confirmation requirement.** Instead of just requiring the next bar
   to close in the same direction, we require the next bar's close to exceed
   the outsized bar's close (for longs) or fall below it (for shorts). This
   ensures the continuation is actually progressing, not just ticking the same
   direction.

2. **Trailing stop instead of fixed target.** #7 used a fixed target at 1.5x
   session range. Here we trail the stop by a fraction of the session range
   each bar, letting winners run further while still cutting losers. The
   target is removed; the position exits only on the trailing stop or at
   session end.

3. **Slightly lower percentile threshold.** #7 used 0.90. We lower it to 0.85
   to get more signals, since the stronger confirmation requirement already
   filters many false positives.

The signal remains: a 5m bar whose body is large relative to the session's
realized range so far, confirmed by the next bar continuing in the same
direction.
"""

from __future__ import annotations

from src.core.types import Bar


class Strategy:
    """Continuation on outsized intra-session moves, with trailing stop."""

    def __init__(self, *, interval: str = "5m", size: int = 1,
                 percentile: float = 0.85, stop_mult: float = 0.75,
                 trail_mult: float = 0.25, min_bars: int = 10):
        self.interval = interval
        self.size = size
        self.percentile = percentile
        self.stop_mult = stop_mult
        self.trail_mult = trail_mult
        self.min_bars = min_bars
        self._session = None
        self._session_bars: list[Bar] = []
        self._session_ratios: list[float] = []
        self._pending_direction = 0
        self._pending_ratio = 0.0
        self._pending_stop = 0.0
        self._entry_price = 0.0
        self._trail_stop = 0.0
        self._bars_held = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if ctx.session != self._session:
            self._session = ctx.session
            self._session_bars = []
            self._session_ratios = []
            self._pending_direction = 0
            self._pending_ratio = 0.0
            self._pending_stop = 0.0
            self._entry_price = 0.0
            self._trail_stop = 0.0
            self._bars_held = 0

        history = ctx.history(self.interval)
        if len(history) < 1:
            return
        last = history[-1]
        if last.session != ctx.session:
            return
        self._session_bars.append(last)

        if not ctx.actionable:
            return

        # Manage open position with trailing stop.
        if ctx.position:
            self._manage_position(ctx)
            return

        if ctx.working_order_ids:
            return

        bars = self._session_bars
        if len(bars) < self.min_bars:
            return

        current = bars[-1]
        if current.close == current.open:
            return

        prior = bars[:-1]
        high = max(b.high for b in prior)
        low = min(b.low for b in prior)
        rng = high - low
        if rng <= 0:
            return

        move = abs(current.close - current.open)
        ratio = move / rng

        direction = 1 if current.close > current.open else -1

        # Check if we have a pending outsized bar from the previous bar.
        if self._pending_direction != 0:
            # Confirm the pending signal: next bar must continue past the
            # outsized bar's close.
            if direction == self._pending_direction:
                if direction > 0 and current.close > self._pending_ratio:
                    pass  # will enter below
                elif direction < 0 and current.close < self._pending_ratio:
                    pass
                else:
                    self._pending_direction = 0
                    self._pending_ratio = 0.0
                    self._pending_stop = 0.0
                    return
            else:
                self._pending_direction = 0
                self._pending_ratio = 0.0
                self._pending_stop = 0.0
                return

            # Entry confirmed.
            stop = rng * self.stop_mult
            if direction > 0:
                ctx.buy(size=self.size, stop_offset=stop,
                        tag="confirmed-trail-long")
            else:
                ctx.sell(size=self.size, stop_offset=stop,
                         tag="confirmed-trail-short")
            self._pending_direction = 0
            self._pending_ratio = 0.0
            self._pending_stop = 0.0
            return

        # Check if this bar is outsized.
        self._session_ratios.append(ratio)
        if len(self._session_ratios) < 5:
            return

        sorted_ratios = sorted(self._session_ratios)
        idx = int(self.percentile * (len(sorted_ratios) - 1))
        threshold = sorted_ratios[idx]

        if ratio >= threshold:
            self._pending_direction = direction
            self._pending_ratio = current.close
            self._pending_stop = rng * self.stop_mult

    def _manage_position(self, ctx) -> None:
        """Trail the stop by trail_mult * session range each bar."""
        bars = self._session_bars
        if len(bars) < 2:
            return
        prior = bars[:-1]
        high = max(b.high for b in prior)
        low = min(b.low for b in prior)
        rng = high - low
        if rng <= 0:
            return

        self._bars_held += 1
        trail_amount = rng * self.trail_mult * self._bars_held

        if ctx.position > 0:
            new_stop = ctx.last_price - trail_amount
            if new_stop > self._trail_stop:
                self._trail_stop = new_stop
                stops = ctx.find_orders(tag="protective-stop")
                for order in stops:
                    ctx.modify(order.id, trigger=self._trail_stop)
        elif ctx.position < 0:
            new_stop = ctx.last_price + trail_amount
            if new_stop < self._trail_stop:
                self._trail_stop = new_stop
                stops = ctx.find_orders(tag="protective-stop")
                for order in stops:
                    ctx.modify(order.id, trigger=self._trail_stop)

    def on_fill(self, ctx) -> None:
        fill = ctx.fill
        if fill.position == 0:
            self._bars_held = 0
            self._trail_stop = 0.0
            return
        # Initialize trailing stop at entry.
        self._entry_price = fill.price
        if fill.position > 0:
            self._trail_stop = fill.price - self._pending_stop
        else:
            self._trail_stop = fill.price + self._pending_stop
        self._bars_held = 0

    def on_lifecycle(self, ctx) -> None:
        pass
