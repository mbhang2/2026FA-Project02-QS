"""Opening-range breakout with prior-session bias, entering on a pullback
that retraces a fixed fraction of the breakout bar's range.

This variant builds on #19 (+0.2149) but adds a trailing stop managed in
on_fill to lock in profits on the breakout move. The bracket target remains
as a fallback, but the protective stop is trailed upward after a favorable
move of at least one opening range. This aims to capture more of the move
when the breakout holds, while still cutting losses when the range wins.

Changes from #19:
- Keep the pullback limit entry with patience cancellation.
- Add a trailing stop that activates after price moves favorably by
  one opening range, then trails at a fixed distance.
- Stop is modified via ctx.modify in on_bars, found by tag.
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
        stop_mult: float = 0.5,
        target_mult: float = 1.5,
        pullback_mult: float = 0.5,
        patience_bars: int = 3,
        trail_activation_mult: float = 1.0,
        trail_distance_mult: float = 0.5,
    ):
        self.interval = interval
        self.daily_interval = daily_interval
        self.opening_minutes = opening_minutes
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.pullback_mult = pullback_mult
        self.patience_bars = patience_bars
        self.trail_activation_mult = trail_activation_mult
        self.trail_distance_mult = trail_distance_mult

        self._session = None
        self._opening_high = None
        self._opening_low = None
        self._opening_range = None
        self._session_bars = 0
        self._breakout_direction = None
        self._limit_price = None
        self._entry_tag = None
        self._bars_since_breakout = 0
        self._entry_price = None
        self._trail_activated = False
        self._stop_tag = "protective-stop"

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        ctx.subscribe(self.daily_interval)

    def on_bars(self, ctx) -> None:
        if self.interval not in ctx.bars:
            return

        if not ctx.actionable:
            return

        if ctx.session != self._session:
            self._session = ctx.session
            self._opening_high = None
            self._opening_low = None
            self._opening_range = None
            self._session_bars = 0
            self._breakout_direction = None
            self._limit_price = None
            self._entry_tag = None
            self._bars_since_breakout = 0
            self._entry_price = None
            self._trail_activated = False

        self._session_bars += 1
        bar = ctx.bars[self.interval]

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

        # Manage trailing stop if in a position
        if ctx.position:
            self._manage_trail(ctx)
            return

        if ctx.working_order_ids:
            self._bars_since_breakout += 1
            if self._bars_since_breakout >= self.patience_bars:
                ctx.cancel_all()
                self._breakout_direction = None
                self._limit_price = None
                self._entry_tag = None
                self._bars_since_breakout = 0
            return

        if self._breakout_direction is None:
            daily = ctx.history(self.daily_interval)
            if len(daily) < 2:
                return
            prev = daily[-2]
            bias = prev.close - prev.open
            if bias == 0:
                return

            bar_range = bar.high - bar.low
            if bar_range <= 0:
                return

            if bar.close > self._opening_high and bias > 0:
                self._breakout_direction = "long"
                self._limit_price = bar.close - self.pullback_mult * bar_range
                self._entry_tag = "pullback-long"
                self._bars_since_breakout = 0
            elif bar.close < self._opening_low and bias < 0:
                self._breakout_direction = "short"
                self._limit_price = bar.close + self.pullback_mult * bar_range
                self._entry_tag = "pullback-short"
                self._bars_since_breakout = 0
            else:
                return

        if self._breakout_direction == "long":
            if ctx.last_price > self._limit_price:
                ctx.buy(
                    size=self.size,
                    type="limit",
                    limit=self._limit_price,
                    stop_offset=self.stop_mult * self._opening_range,
                    target_offset=self.target_mult * self._opening_range,
                    tag=self._entry_tag,
                )
        else:
            if ctx.last_price < self._limit_price:
                ctx.sell(
                    size=self.size,
                    type="limit",
                    limit=self._limit_price,
                    stop_offset=self.stop_mult * self._opening_range,
                    target_offset=self.target_mult * self._opening_range,
                    tag=self._entry_tag,
                )

    def _manage_trail(self, ctx) -> None:
        if self._entry_price is None:
            return

        stops = ctx.find_orders(tag=self._stop_tag)
        if not stops:
            return

        stop_order = stops[0]
        direction = 1 if ctx.position > 0 else -1

        if not self._trail_activated:
            # Activate after price moves favorably by activation_mult * opening_range
            favorable_move = (ctx.last_price - self._entry_price) * direction
            if favorable_move >= self.trail_activation_mult * self._opening_range:
                self._trail_activated = True

        if self._trail_activated:
            # Trail at fixed distance from current price
            new_stop = ctx.last_price - direction * self.trail_distance_mult * self._opening_range
            current_stop = stop_order.stop_price
            # Only move stop in favorable direction
            if direction > 0 and new_stop > current_stop:
                ctx.modify(stop_order.id, trigger=new_stop)
            elif direction < 0 and new_stop < current_stop:
                ctx.modify(stop_order.id, trigger=new_stop)

    def on_fill(self, ctx) -> None:
        fill = ctx.fill
        if fill.position == 0:
            self._entry_price = None
            self._trail_activated = False
            return
        # Record entry price when position opens
        if self._entry_price is None:
            self._entry_price = fill.price
            self._trail_activated = False

    def on_lifecycle(self, ctx) -> None:
        pass
