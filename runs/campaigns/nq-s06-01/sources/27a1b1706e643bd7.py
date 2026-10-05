"""Opening-range breakout with prior-session bias, entering on a pullback
that retraces a fixed fraction of the breakout bar's range, with a
time-based cancellation of the limit order if it does not fill within a
few bars.

This variant changes the pullback entry from a fixed fraction of the breakout
bar's range to a fraction of the opening range. The idea is that the opening
range is a more stable measure of expected volatility than the single breakout
bar, which can be noisy. The pullback level is set at 0.25 * opening_range
away from the breakout close, in the direction of the pullback.

Changes from #19 (best so far, +0.2149):
- Pullback distance is now 0.25 * opening_range instead of 0.5 * breakout bar range.
- This should provide a more consistent pullback level and potentially
  improve the fill rate and score.
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
        pullback_fraction: float = 0.25,
        patience_bars: int = 3,
    ):
        self.interval = interval
        self.daily_interval = daily_interval
        self.opening_minutes = opening_minutes
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.pullback_fraction = pullback_fraction
        self.patience_bars = patience_bars

        # State per session
        self._session = None
        self._opening_high = None
        self._opening_low = None
        self._opening_range = None
        self._session_bars = 0
        self._breakout_direction = None  # 'long' or 'short' after breakout
        self._limit_price = None
        self._entry_tag = None
        self._bars_since_breakout = 0

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
            self._bars_since_breakout = 0

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

        # If we already have a position, the bracket handles exits.
        if ctx.position:
            return

        # If we have a working limit order, check if it should be cancelled.
        if ctx.working_order_ids:
            self._bars_since_breakout += 1
            if self._bars_since_breakout >= self.patience_bars:
                # Cancel all working orders (should be just our limit entry)
                ctx.cancel_all()
                # Reset breakout state to allow a new breakout detection
                self._breakout_direction = None
                self._limit_price = None
                self._entry_tag = None
                self._bars_since_breakout = 0
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

            if bar.close > self._opening_high and bias > 0:
                self._breakout_direction = "long"
                # Place limit below current price, using a fraction of opening range.
                self._limit_price = bar.close - self.pullback_fraction * self._opening_range
                self._entry_tag = "pullback-long"
                self._bars_since_breakout = 0
            elif bar.close < self._opening_low and bias < 0:
                self._breakout_direction = "short"
                self._limit_price = bar.close + self.pullback_fraction * self._opening_range
                self._entry_tag = "pullback-short"
                self._bars_since_breakout = 0
            else:
                return

        # We have a breakout direction; place the limit order.
        if self._breakout_direction == "long":
            # Only place if price is above our limit (i.e., we are waiting for a pullback).
            if ctx.last_price > self._limit_price:
                ctx.buy(
                    size=self.size,
                    type="limit",
                    limit=self._limit_price,
                    stop_offset=self.stop_mult * self._opening_range,
                    target_offset=self.target_mult * self._opening_range,
                    tag=self._entry_tag,
                )
        else:  # short
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
