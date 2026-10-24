"""One entry at the first actionable instant, direction fitted from the full
training tick series, and held to the end of the run.

Prior trials show that turnover is expensive and that a single early position
without exits is consistently the best or near-best performing shape.  The
remaining choice is *which side* to hold.  Earlier versions fitted the sign
from 5-minute bars, which can differ from the actual first-tick to last-tick
drift this strategy realizes.  This version fits that sign from the raw
training ticks available in setup(): long if the last training tick is above
the first, short otherwise.

The entry is a GTC market order submitted at the first session_open event, so
it becomes eligible at the first tick and survives session boundaries.  No
stops, targets, or manual exits are used: one fill, continuous exposure.
"""

from __future__ import annotations


class Strategy:
    """Single fitted-direction market entry, held to the end."""

    def __init__(self, *, interval: str = "5m", size: int = 1) -> None:
        self.interval = interval
        self.size = size
        self.direction = 0
        self._entered = False

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        self.direction = self._fit_direction(ctx)

    def _fit_direction(self, ctx) -> int:
        """Return 1 for long, -1 for short, from full-period tick drift."""
        # Raw ticks are the most faithful measure of the entry-to-exit drift
        # for a strategy that enters at the first tick and holds to the end.
        try:
            ticks = ctx.ticks()
        except Exception:
            ticks = ()

        if ticks:
            drift = ticks[-1].price - ticks[0].price
            if drift > 0:
                return 1
            if drift < 0:
                return -1

        # Fallback if raw ticks are unavailable or perfectly flat: use the
        # open/close balance of training bars.
        try:
            bars = ctx.train_bars(self.interval)
            if bars:
                up = sum(1 for b in bars if b.close > b.open)
                down = sum(1 for b in bars if b.close < b.open)
                if up > down:
                    return 1
                if down > up:
                    return -1
        except Exception:
            pass

        return 1

    def _try_enter(self, ctx) -> None:
        """Submit the single entry if nothing is working and we are flat."""
        if not ctx.actionable:
            return
        if self._entered or ctx.position or ctx.working_order_ids:
            return
        if self.direction == 0:
            return

        if self.direction > 0:
            ctx.buy(size=self.size, tif="GTC", tag="drift-entry-long")
        else:
            ctx.sell(size=self.size, tif="GTC", tag="drift-entry-short")

    def on_bars(self, ctx) -> None:
        # Fallback retry on regular bars in case the session_open attempt was
        # not actionable or did not fill.  Once a position exists this no-ops.
        self._try_enter(ctx)

    def on_fill(self, ctx) -> None:
        if ctx.fill.position != 0:
            self._entered = True

    def on_lifecycle(self, ctx) -> None:
        # Try at the earliest possible instant: the first session_open event.
        if ctx.event.kind == "session_open":
            self._try_enter(ctx)
