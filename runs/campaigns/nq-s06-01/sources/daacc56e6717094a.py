"""Opening-range breakout with prior-session bias, but entering on a pullback
that retraces a fixed fraction of the breakout bar's range, rather than a
fraction of the opening range.

The campaign question: when price leaves the opening range, does the move hold
or does the range win? This strategy answers "the move holds" in the direction
of the prior session's bias, on the ordinary run of sessions.

Differences from #8 (best so far, +0.0647):
- The pullback limit price is set relative to the breakout bar's close, not
  relative to the opening range. Specifically, for a long breakout we place a
  limit at the breakout bar's close minus a fraction of that bar's range
  (high - low). For a short breakout, we place a limit at the breakout bar's
  close plus a fraction of that bar's range.
- This ties the pullback depth to the immediate volatility of the breakout bar
  rather than the opening range, which may be too wide or too narrow for the
  actual move.
- The bracket offsets remain based on the opening range, as in #8.
- We also add a minimum breakout bar range filter: if the breakout bar's range
  is zero (doji), we skip the entry, because a pullback from a doji is
  meaningless.

This acts on most sessions: the only filters are prior-session direction, a
non-zero breakout bar range, and the breakout itself. The limit entry improves
the average fill price compared to a market entry.
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

        # If we already have a working limit order, do nothing.
        if ctx.working_order_ids:
            return

        # If we already have a position, the bracket handles exits.
        if ctx.position:
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
                # Place limit below current price, using the breakout bar's range.
                self._limit_price = bar.close - self.pullback_mult * bar_range
                self._entry_tag = "pullback-long"
            elif bar.close < self._opening_low and bias < 0:
                self._breakout_direction = "short"
                self._limit_price = bar.close + self.pullback_mult * bar_range
                self._entry_tag = "pullback-short"
            else:
                return

        # We have a breakout direction; place or replace the limit order.
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
