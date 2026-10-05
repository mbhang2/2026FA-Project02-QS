"""Opening-range breakout with prior-session bias, entering on a pullback
that retraces a fixed fraction of the breakout bar's range.

This variant simplifies #44 (+0.2191) by removing the adaptive bias
strength scaling and instead using a fixed breakout strength filter of
0.25 * opening range, which was the best-performing fixed filter in the
family (#42). It keeps the pullback limit entry, patience cancellation,
and the prior-session bias direction filter. The goal is to verify that
the simpler fixed filter is as good as the adaptive one while reducing
parameter surface.

Changes from #44:
- Remove `bias_strength_mult` and `max_pullback_mult` adaptive scaling.
- Use fixed `breakout_strength = 0.25` and `max_pullback_distance = 0.5`
  from #42.
- Keep patience at 3 bars.
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
        breakout_strength: float = 0.25,
        max_pullback_distance: float = 0.5,
        patience_bars: int = 3,
    ):
        self.interval = interval
        self.daily_interval = daily_interval
        self.opening_minutes = opening_minutes
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.pullback_mult = pullback_mult
        self.breakout_strength = breakout_strength
        self.max_pullback_distance = max_pullback_distance
        self.patience_bars = patience_bars

        self._session = None
        self._opening_high = None
        self._opening_low = None
        self._opening_range = None
        self._session_bars = 0
        self._breakout_direction = None
        self._limit_price = None
        self._entry_tag = None
        self._bars_since_breakout = 0

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

            strength_threshold = self.breakout_strength * self._opening_range
            max_pullback = self.max_pullback_distance * self._opening_range

            if bar.close > self._opening_high and bias > 0:
                if bar.close - self._opening_high < strength_threshold:
                    return
                pullback_distance = self.pullback_mult * bar_range
                if pullback_distance > max_pullback:
                    return
                self._breakout_direction = "long"
                self._limit_price = bar.close - pullback_distance
                self._entry_tag = "pullback-long"
                self._bars_since_breakout = 0
            elif bar.close < self._opening_low and bias < 0:
                if self._opening_low - bar.close < strength_threshold:
                    return
                pullback_distance = self.pullback_mult * bar_range
                if pullback_distance > max_pullback:
                    return
                self._breakout_direction = "short"
                self._limit_price = bar.close + pullback_distance
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

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
