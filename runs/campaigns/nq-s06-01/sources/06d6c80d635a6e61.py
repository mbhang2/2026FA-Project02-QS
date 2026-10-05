"""Opening-range breakout with prior-session bias, entering on a pullback
that retraces a fixed fraction of the breakout bar's range, with a
time-based cancellation of the limit order if it does not fill within a
few bars.

This variant builds on #19 (best so far, +0.2149) but adds a volatility
filter: it only takes a breakout if the opening range is above a small
fraction of the prior session's range. The idea is that a very small
opening range may produce noisy breakouts that fail to hold, while a
larger opening range indicates real participation and a more reliable
move. The filter is deliberately loose to keep the strategy acting on
the ordinary run of sessions.

Changes from #19:
- Compute prior session's daily range.
- Require opening_range >= 0.5 * prior_daily_range before acting.
- All other logic unchanged.
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
        min_range_fraction: float = 0.5,
    ):
        self.interval = interval
        self.daily_interval = daily_interval
        self.opening_minutes = opening_minutes
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.pullback_mult = pullback_mult
        self.patience_bars = patience_bars
        self.min_range_fraction = min_range_fraction

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
        self._prior_daily_range = None

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
            self._prior_daily_range = None

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
                    # Fetch prior session's daily range at this point.
                    daily = ctx.history(self.daily_interval)
                    if len(daily) >= 2:
                        prev = daily[-2]
                        self._prior_daily_range = prev.high - prev.low
            return

        if self._opening_range is None or self._opening_range <= 0:
            return

        # Volatility filter: require opening range to be at least a fraction
        # of the prior session's range.
        if (
            self._prior_daily_range is not None
            and self._prior_daily_range > 0
            and self._opening_range < self.min_range_fraction * self._prior_daily_range
        ):
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

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
