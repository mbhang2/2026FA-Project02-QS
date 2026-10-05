"""Opening-range breakout with prior-session bias, entering on a pullback
that retraces a fixed fraction of the breakout bar's range, with a
time-based cancellation. This variant adds a volatility filter: only take
the breakout if the opening range is large enough relative to the prior
session's range, and only if the breakout bar's range is not too large
relative to the opening range (avoiding exhaustion spikes).

The campaign question: when price leaves the opening range, does the move hold
or does the range win? This strategy answers "the move holds" in the direction
of the prior session's bias, on the ordinary run of sessions.

Changes from #19 (best so far, +0.2149):
- Add a minimum opening range filter: require the opening range to be at
  least 0.5 times the prior session's range. This filters out quiet opens
  that are more likely to be noise.
- Add a maximum breakout bar range filter: reject breakouts whose bar range
  is more than 2.0 times the opening range. This filters out exhaustion
  spikes that often reverse.
- Keep the pullback entry at 0.5 * breakout bar range.
- Keep the time-based cancellation at 3 bars.
- Keep the bracket offsets based on the opening range.
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
        min_opening_range_mult: float = 0.5,
        max_breakout_range_mult: float = 2.0,
    ):
        self.interval = interval
        self.daily_interval = daily_interval
        self.opening_minutes = opening_minutes
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.pullback_mult = pullback_mult
        self.patience_bars = patience_bars
        self.min_opening_range_mult = min_opening_range_mult
        self.max_breakout_range_mult = max_breakout_range_mult

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
        self._prior_session_range = None

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
            self._prior_session_range = None

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
                    # Get prior session's daily bar for range filter.
                    daily = ctx.history(self.daily_interval)
                    if len(daily) >= 2:
                        prev = daily[-2]
                        self._prior_session_range = prev.high - prev.low
            return

        if self._opening_range is None or self._opening_range <= 0:
            return

        # Volatility filter: opening range must be large enough.
        if self._prior_session_range is not None and self._prior_session_range > 0:
            if self._opening_range < self.min_opening_range_mult * self._prior_session_range:
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

            bar_range = bar.high - bar.low
            if bar_range <= 0:
                return  # doji breakout bar, no meaningful pullback level

            # Reject exhaustion spikes: breakout bar range too large.
            if bar_range > self.max_breakout_range_mult * self._opening_range:
                return

            if bar.close > self._opening_high and bias > 0:
                self._breakout_direction = "long"
                # Place limit below current price, using the breakout bar's range.
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
