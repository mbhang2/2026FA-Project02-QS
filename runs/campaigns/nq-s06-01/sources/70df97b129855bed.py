"""Opening-range breakout with prior-session bias, entering on a pullback
and using a trailing stop that activates after a threshold move.

The campaign question: when price leaves the opening range, does the move hold
or does the range win? This strategy answers "the move holds" in the direction
of the prior session's bias, on the ordinary run of sessions.

Differences from #15 (best so far, +0.1018):
- Instead of a fixed target offset, we use a trailing stop that activates once
  the price has moved in our favor by a threshold (trail_trigger_mult * opening_range).
- The initial stop is tighter (stop_mult=0.35) to reduce the loss on failed
  breakouts.
- The trailing stop distance is trail_mult * opening_range, so as the price
  moves in our favor, the stop follows at a fixed distance.
- The target is removed entirely; we rely on the trailing stop to capture
  the move and exit. This avoids the fixed target that may cut winners short
  on strong trending days.

This acts on most sessions: the only filters are prior-session direction and
the breakout itself.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        daily_interval: str = "1d",
        opening_minutes: int = 30,
        size: int = 1,
        stop_mult: float = 0.35,
        trail_mult: float = 0.5,
        trail_trigger_mult: float = 0.8,
        pullback_mult: float = 0.5,
    ):
        self.interval = interval
        self.daily_interval = daily_interval
        self.opening_minutes = opening_minutes
        self.size = size
        self.stop_mult = stop_mult
        self.trail_mult = trail_mult
        self.trail_trigger_mult = trail_trigger_mult
        self.pullback_mult = pullback_mult

        # State per session
        self._session = None
        self._opening_high = None
        self._opening_low = None
        self._opening_range = None
        self._session_bars = 0
        self._breakout_direction = None  # 'long' or 'short' after breakout
        self._limit_price = None
        self._entry_tag = None
        self._trailing_active = False
        self._highest_price = None
        self._lowest_price = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        ctx.subscribe(self.daily_interval)

    def on_bars(self, ctx) -> None:
        # Daily bar closes at session end -- not a decision point for entries.
        if self.interval not in ctx.bars:
            return

        if not ctx.actionable:
            return

        # Session tracking
        if ctx.session != self._session:
            self._session = ctx.session
            self._opening_high = None
            self._opening_low = None
            self._opening_range = None
            self._session_bars = 0
            self._breakout_direction = None
            self._limit_price = None
            self._entry_tag = None
            self._trailing_active = False
            self._highest_price = None
            self._lowest_price = None

        self._session_bars += 1
        bar = ctx.bars[self.interval]

        # Build the opening range from the first `opening_minutes` of bars.
        opening_bars = self.opening_minutes // 5
        if self._session_bars <= opening_bars:
            if self._opening_high is None or bar.high > self._opening_high:
                self._opening_high = bar.high
            if self._opening_low is None or bar.low < self._opening_low:
                self._opening_low = bar.low
            if self._session_bars == opening_bars:
                if self._opening_high is not None and self._opening_low is not None:
                    self._opening_range = self._opening_high - self._opening_low
            return

        if self._opening_range is None or self._opening_range <= 0:
            return

        # If we have a position, manage the trailing stop.
        if ctx.position:
            self._manage_trailing_stop(ctx)
            return

        # If we already have a working limit order, do nothing.
        if ctx.working_order_ids:
            return

        # If we have not yet detected a breakout, look for one.
        if self._breakout_direction is None:
            # Prior session daily bar for direction.
            daily = ctx.history(self.daily_interval)
            if len(daily) < 2:
                return
            prev = daily[-2]
            bias = prev.close - prev.open
            if bias == 0:
                return

            bar_range = bar.high - bar.low
            if bar_range <= 0:
                return  # doji breakout bar, no meaningful pullback level

            if bar.close > self._opening_high and bias > 0:
                self._breakout_direction = "long"
                self._limit_price = bar.close - self.pullback_mult * bar_range
                self._entry_tag = "pullback-long"
            elif bar.close < self._opening_low and bias < 0:
                self._breakout_direction = "short"
                self._limit_price = bar.close + self.pullback_mult * bar_range
                self._entry_tag = "pullback-short"
            else:
                return

        # We have a breakout direction; place the limit order.
        if self._breakout_direction == "long":
            if ctx.last_price > self._limit_price:
                ctx.buy(
                    size=self.size,
                    type="limit",
                    limit=self._limit_price,
                    stop_offset=self.stop_mult * self._opening_range,
                    tag=self._entry_tag,
                )
        else:  # short
            if ctx.last_price < self._limit_price:
                ctx.sell(
                    size=self.size,
                    type="limit",
                    limit=self._limit_price,
                    stop_offset=self.stop_mult * self._opening_range,
                    tag=self._entry_tag,
                )

    def _manage_trailing_stop(self, ctx) -> None:
        """Update the trailing stop once the price has moved in our favor."""
        if self._breakout_direction == "long":
            if self._highest_price is None or ctx.last_price > self._highest_price:
                self._highest_price = ctx.last_price
            if not self._trailing_active:
                if self._limit_price is not None and self._opening_range is not None:
                    if self._highest_price >= self._limit_price + self.trail_trigger_mult * self._opening_range:
                        self._trailing_active = True
                        # Replace the initial stop with a trailing stop.
                        stops = ctx.find_orders(tag=self._entry_tag + "-stop")
                        if stops:
                            new_stop = self._highest_price - self.trail_mult * self._opening_range
                            ctx.modify(stops[0].id, trigger=new_stop)
            else:
                if self._opening_range is not None:
                    new_stop = self._highest_price - self.trail_mult * self._opening_range
                    stops = ctx.find_orders(tag=self._entry_tag + "-stop")
                    if stops:
                        current_stop = stops[0].stop_price
                        if new_stop > current_stop:
                            ctx.modify(stops[0].id, trigger=new_stop)
        else:  # short
            if self._lowest_price is None or ctx.last_price < self._lowest_price:
                self._lowest_price = ctx.last_price
            if not self._trailing_active:
                if self._limit_price is not None and self._opening_range is not None:
                    if self._lowest_price <= self._limit_price - self.trail_trigger_mult * self._opening_range:
                        self._trailing_active = True
                        stops = ctx.find_orders(tag=self._entry_tag + "-stop")
                        if stops:
                            new_stop = self._lowest_price + self.trail_mult * self._opening_range
                            ctx.modify(stops[0].id, trigger=new_stop)
            else:
                if self._opening_range is not None:
                    new_stop = self._lowest_price + self.trail_mult * self._opening_range
                    stops = ctx.find_orders(tag=self._entry_tag + "-stop")
                    if stops:
                        current_stop = stops[0].stop_price
                        if new_stop < current_stop:
                            ctx.modify(stops[0].id, trigger=new_stop)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
