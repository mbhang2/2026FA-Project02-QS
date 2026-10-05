"""Opening-range breakout with prior-session bias, entering on a pullback
that retraces a fixed fraction of the breakout bar's range, with a
time-based cancellation and a trailing stop managed in on_fill.

The campaign question: when price leaves the opening range, does the move hold
or does the range win? This strategy answers "the move holds" in the direction
of the prior session's bias, on the ordinary run of sessions.

Changes from #27:
- Use a more patient cancellation (patience_bars=5) to give the pullback
  more time to fill.
- Increase target_mult to 2.0 to let winners run further.
- Adjust stop_mult to 0.75 to give a bit more room.
- Keep the trailing stop with trail_mult=0.25.
- The trailing stop is managed in on_bars when a position is open.
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
        stop_mult: float = 0.75,
        target_mult: float = 2.0,
        pullback_mult: float = 0.5,
        patience_bars: int = 5,
        trail_mult: float = 0.25,
    ):
        self.interval = interval
        self.daily_interval = daily_interval
        self.opening_minutes = opening_minutes
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.pullback_mult = pullback_mult
        self.patience_bars = patience_bars
        self.trail_mult = trail_mult

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

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        ctx.subscribe(self.daily_interval)

    def on_bars(self, ctx) -> None:
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
            self._bars_since_breakout = 0
            self._best_price = None

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

    def _manage_trailing_stop(self, ctx) -> None:
        if self._opening_range is None or self._opening_range <= 0:
            return

        if ctx.position > 0:
            if self._best_price is None or ctx.last_price > self._best_price:
                self._best_price = ctx.last_price
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
        if fill.position != 0:
            self._best_price = fill.price
        else:
            self._best_price = None

    def on_lifecycle(self, ctx) -> None:
        pass
