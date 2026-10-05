"""Opening-range breakout with prior-session bias, entering on a pullback
that retraces a fraction of the breakout bar's range, but with the limit
order placed only after the breakout bar has closed and the next bar has
confirmed the pullback.

The campaign question: when price leaves the opening range, does the move hold
or does the range win? This strategy answers "the move holds" in the direction
of the prior session's bias, on the ordinary run of sessions.

Differences from #15 (best so far, +0.1018):
- Instead of placing a limit order immediately after the breakout bar closes,
  we wait for the next 5-minute bar to close. If that bar's close is below
  the breakout bar's close for a long (or above for a short), we consider
  the pullback to have started, and we place the limit order at the
  pullback bar's close minus (or plus) a fraction of the pullback bar's
  range. This adds a confirmation step that filters out immediate
  continuation moves where the limit would never fill.
- The pullback fraction is applied to the pullback bar's range, not the
  breakout bar's range, to adapt to the volatility of the pullback itself.
- The bracket offsets remain based on the opening range, as in #15.
- We keep the minimum range filter on both the breakout bar and the
  pullback bar.

This acts on most sessions: the only filters are prior-session direction,
non-zero ranges, the breakout, and the pullback confirmation. The limit
entry improves the average fill price compared to a market entry.
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
    ):
        self.interval = interval
        self.daily_interval = daily_interval
        self.opening_minutes = opening_minutes
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.pullback_mult = pullback_mult

        # State per session
        self._session = None
        self._opening_high = None
        self._opening_low = None
        self._opening_range = None
        self._session_bars = 0
        self._breakout_direction = None  # 'long' or 'short' after breakout
        self._breakout_bar_close = None
        self._pending_pullback = False  # waiting for the pullback bar to close
        self._limit_price = None
        self._entry_tag = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        ctx.subscribe(self.daily_interval)

    def on_bars(self, ctx) -> None:
        # Daily bar closes at session end -- not a decision point for entries.
        if self.interval not in ctx.bars:
            return

        if not ctx.actionable:
            return

        # One position at a time; the bracket handles exits.
        if ctx.position or ctx.working_order_ids:
            return

        # Session tracking
        if ctx.session != self._session:
            self._session = ctx.session
            self._opening_high = None
            self._opening_low = None
            self._opening_range = None
            self._session_bars = 0
            self._breakout_direction = None
            self._breakout_bar_close = None
            self._pending_pullback = False
            self._limit_price = None
            self._entry_tag = None

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

        # Prior session daily bar for direction.
        daily = ctx.history(self.daily_interval)
        if len(daily) < 2:
            return
        prev = daily[-2]
        bias = prev.close - prev.open
        if bias == 0:
            return

        # If we are waiting for a pullback bar to close after a breakout.
        if self._pending_pullback:
            bar_range = bar.high - bar.low
            if bar_range <= 0:
                # Doji pullback bar: reset and look for a new breakout.
                self._breakout_direction = None
                self._breakout_bar_close = None
                self._pending_pullback = False
                return

            if self._breakout_direction == "long":
                # Pullback confirmed if this bar closes below the breakout bar's close.
                if bar.close < self._breakout_bar_close:
                    # Place limit below the pullback bar's close.
                    self._limit_price = bar.close - self.pullback_mult * bar_range
                    self._entry_tag = "pullback-long"
                    self._pending_pullback = False
                    # Fall through to place the order below.
                else:
                    # Pullback did not happen; reset.
                    self._breakout_direction = None
                    self._breakout_bar_close = None
                    self._pending_pullback = False
                    return
            else:  # short
                if bar.close > self._breakout_bar_close:
                    self._limit_price = bar.close + self.pullback_mult * bar_range
                    self._entry_tag = "pullback-short"
                    self._pending_pullback = False
                else:
                    self._breakout_direction = None
                    self._breakout_bar_close = None
                    self._pending_pullback = False
                    return

        # If we have a pending limit order to place.
        if self._limit_price is not None:
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
                    # Reset for next opportunity.
                    self._breakout_direction = None
                    self._breakout_bar_close = None
                    self._limit_price = None
                    self._entry_tag = None
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
                    self._breakout_direction = None
                    self._breakout_bar_close = None
                    self._limit_price = None
                    self._entry_tag = None
            return

        # Look for a new breakout.
        if self._breakout_direction is None:
            bar_range = bar.high - bar.low
            if bar_range <= 0:
                return  # doji breakout bar, no meaningful pullback level

            if bar.close > self._opening_high and bias > 0:
                self._breakout_direction = "long"
                self._breakout_bar_close = bar.close
                self._pending_pullback = True
            elif bar.close < self._opening_low and bias < 0:
                self._breakout_direction = "short"
                self._breakout_bar_close = bar.close
                self._pending_pullback = True

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
