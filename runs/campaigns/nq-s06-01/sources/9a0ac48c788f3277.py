"""Opening-range breakout with a trailing stop, entering on the breakout bar.

The campaign question: when price leaves the opening range, does the move hold
or does the range win? This strategy answers "the move holds" and enters on the
breakout bar close, but manages the exit with a trailing stop instead of a
fixed target.

Mechanics:
- Opening range = first 30 minutes of the session, measured on 5-minute bars.
- A breakout is a 5-minute bar that closes beyond the opening range high or low.
- Direction filter: prior session's daily bar must agree -- up prior session
  means only long breakouts, down prior session means only short breakouts.
- Entry on the close of the breakout bar, with a protective stop offset.
- Instead of a fixed target, the stop is trailed by a fixed percentage of the
  opening range after the position is in profit.

This acts on most sessions: the only filters are prior-session direction and
the breakout itself. The trailing stop lets winners run while still protecting
against a reversal back into the range.
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
        initial_stop_mult: float = 0.5,
        trail_mult: float = 0.25,
    ):
        self.interval = interval
        self.daily_interval = daily_interval
        self.opening_minutes = opening_minutes
        self.size = size
        self.initial_stop_mult = initial_stop_mult
        self.trail_mult = trail_mult

        # State per session
        self._session = None
        self._opening_high = None
        self._opening_low = None
        self._opening_range = None
        self._session_bars = 0
        self._entry_price = None
        self._stop_price = None
        self._direction = None  # 'long' or 'short'

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
            self._entry_price = None
            self._stop_price = None
            self._direction = None

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

        # If we have a position, manage the trailing stop.
        if ctx.position:
            if self._direction == "long":
                new_stop = ctx.last_price - self.trail_mult * self._opening_range
                if self._stop_price is None or new_stop > self._stop_price:
                    self._stop_price = new_stop
                    stops = ctx.find_orders(tag="protective-stop")
                    if stops:
                        ctx.modify(stops[0].id, trigger=self._stop_price)
            elif self._direction == "short":
                new_stop = ctx.last_price + self.trail_mult * self._opening_range
                if self._stop_price is None or new_stop < self._stop_price:
                    self._stop_price = new_stop
                    stops = ctx.find_orders(tag="protective-stop")
                    if stops:
                        ctx.modify(stops[0].id, trigger=self._stop_price)
            return

        # If we have a working order, do nothing.
        if ctx.working_order_ids:
            return

        # Prior session daily bar for direction.
        daily = ctx.history(self.daily_interval)
        if len(daily) < 2:
            return
        prev = daily[-2]
        bias = prev.close - prev.open
        if bias == 0:
            return

        # Breakout detection on the current bar's close.
        if bar.close > self._opening_high and bias > 0:
            self._direction = "long"
            self._entry_price = bar.close
            self._stop_price = self._entry_price - self.initial_stop_mult * self._opening_range
            ctx.buy(
                size=self.size,
                stop=self._stop_price,
                tag="entry-long",
            )
        elif bar.close < self._opening_low and bias < 0:
            self._direction = "short"
            self._entry_price = bar.close
            self._stop_price = self._entry_price + self.initial_stop_mult * self._opening_range
            ctx.sell(
                size=self.size,
                stop=self._stop_price,
                tag="entry-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
