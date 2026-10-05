"""Opening-range breakout with a prior-session bias, but with a mean-reversion
entry on the first pullback after the breakout.

The campaign question: when price leaves the opening range, does the move hold
or does the range win? This strategy answers "the move holds" but enters on a
pullback, not on the breakout bar itself, to get a better fill price.

Mechanics:
- The opening range is the first 30 minutes of the regular-hours session,
  measured on 5-minute bars.
- A breakout is a 5-minute bar that closes beyond the opening range high or low.
- Direction filter: the prior session's daily bar must agree -- up prior session
  means only long breakouts, down prior session means only short breakouts.
- Instead of entering on the breakout bar close, we wait for a pullback: after
  a bullish breakout, we place a limit order below the current price; after a
  bearish breakout, above. The limit is set at a fixed fraction of the opening
  range back inside the range.
- The limit order has a protective bracket sized from the opening range width.

This acts on most sessions: the only filters are prior-session direction and
the breakout itself. The limit entry improves the average fill price compared
to the market entry used in prior trials.
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
        pullback_mult: float = 0.25,
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

            if bar.close > self._opening_high and bias > 0:
                self._breakout_direction = "long"
                # Place limit below current price, inside the range.
                self._limit_price = bar.close - self.pullback_mult * self._opening_range
                self._entry_tag = "pullback-long"
            elif bar.close < self._opening_low and bias < 0:
                self._breakout_direction = "short"
                self._limit_price = bar.close + self.pullback_mult * self._opening_range
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
