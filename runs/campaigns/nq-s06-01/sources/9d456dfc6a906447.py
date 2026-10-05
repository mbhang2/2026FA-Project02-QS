"""Opening-range breakout with prior-session bias, entering on a pullback
that retraces a fixed fraction of the breakout bar's range, with a
time-based cancellation and a trailing stop managed in on_fill.

This variant changes the trailing stop logic to trail by a fraction of the
opening range, but only after price has moved favorably by at least one
opening range. This gives the trade room to breathe initially, then locks
in profits if the move holds.

Changes from #30:
- The initial stop remains at 0.5 * opening range from entry.
- The trailing stop is not activated until price has moved at least 1.0 *
  opening range in the direction of the trade.
- Once activated, the trail distance is 0.25 * opening range.
- This aims to avoid being stopped out by normal fluctuations before the
  move has had a chance to develop.
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
        trail_mult: float = 0.25,
        activation_mult: float = 1.0,
        pullback_mult: float = 0.5,
        patience_bars: int = 3,
    ):
        self.interval = interval
        self.daily_interval = daily_interval
        self.opening_minutes = opening_minutes
        self.size = size
        self.stop_mult = stop_mult
        self.trail_mult = trail_mult
        self.activation_mult = activation_mult
        self.pullback_mult = pullback_mult
        self.patience_bars = patience_bars

        # State per session
        self._session = None
        self._opening_high = None
        self._opening_low = None
        self._opening_range = None
        self._session_bars = 0
        self._breakout_direction = None
        self._limit_price = None
        self._entry_tag = None
        self._bars_since_breakout = 0
        self._best_price = None
        self._trailing_active = False

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
            self._best_price = None
            self._trailing_active = False

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

        if ctx.position:
            self._manage_trailing_stop(ctx)
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
                    tag=self._entry_tag,
                )
        else:
            if ctx.last_price < self._limit_price:
                ctx.sell(
                    size=self.size,
                    type="limit",
                    limit=self._limit_price,
                    tag=self._entry_tag,
                )

    def _manage_trailing_stop(self, ctx) -> None:
        if self._opening_range is None or self._opening_range <= 0:
            return

        if ctx.position > 0:
            if self._best_price is None or ctx.last_price > self._best_price:
                self._best_price = ctx.last_price
            if not self._trailing_active:
                if self._best_price >= self._activation_mult * self._opening_range + self._entry_price:
                    self._trailing_active = True
            if self._trailing_active:
                new_stop = self._best_price - self.trail_mult * self._opening_range
                stops = ctx.find_orders(tag="trailing-stop")
                if stops:
                    if new_stop > stops[0].stop_price:
                        ctx.modify(stops[0].id, trigger=new_stop)
                else:
                    ctx.sell(
                        size=abs(ctx.position),
                        type="stop",
                        trigger=new_stop,
                        tif="GTC",
                        reduce_only=True,
                        tag="trailing-stop",
                    )
        elif ctx.position < 0:
            if self._best_price is None or ctx.last_price < self._best_price:
                self._best_price = ctx.last_price
            if not self._trailing_active:
                if self._best_price <= self._entry_price - self._activation_mult * self._opening_range:
                    self._trailing_active = True
            if self._trailing_active:
                new_stop = self._best_price + self.trail_mult * self._opening_range
                stops = ctx.find_orders(tag="trailing-stop")
                if stops:
                    if new_stop < stops[0].stop_price:
                        ctx.modify(stops[0].id, trigger=new_stop)
                else:
                    ctx.buy(
                        size=abs(ctx.position),
                        type="stop",
                        trigger=new_stop,
                        tif="GTC",
                        reduce_only=True,
                        tag="trailing-stop",
                    )

    def on_fill(self, ctx) -> None:
        fill = ctx.fill
        if fill.position == 0:
            self._best_price = None
            self._trailing_active = False
            return

        self._best_price = fill.price
        self._entry_price = fill.price
        self._trailing_active = False

        if self._opening_range is None or self._opening_range <= 0:
            return

        if fill.position > 0:
            initial_stop = fill.price - self.stop_mult * self._opening_range
            ctx.sell(
                size=abs(fill.position),
                type="stop",
                trigger=initial_stop,
                tif="GTC",
                reduce_only=True,
                tag="trailing-stop",
            )
        else:
            initial_stop = fill.price + self.stop_mult * self._opening_range
            ctx.buy(
                size=abs(fill.position),
                type="stop",
                trigger=initial_stop,
                tif="GTC",
                reduce_only=True,
                tag="trailing-stop",
            )

    def on_lifecycle(self, ctx) -> None:
        pass
