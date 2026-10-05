"""Opening-range breakout with a stated spread and a three-way exit.

The question: when price leaves the opening range, does the move hold or does
the range win? This strategy takes the "move holds" side on the first
confirmed breakout of the regular-hours opening range, and lets the exit
decompose into three outcomes: target hit, stop hit, or deadline expired.

Opening range: first 30 minutes of the session, measured on 5-minute bars.
Breakout: close strictly beyond the range by at least 0.25 points (one tick).
Entry: stop order at the breakout level, so the fill is the breakout itself.
Exit: target at 1.0x range width, stop at 0.5x range width, deadline 15 bars
(75 minutes) after entry. All fills assume an unfavorable spread of 0.25
points, so a buy fills at price + 0.25 and a sell at price - 0.25, on top of
commission.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        range_bars: int = 6,          # 30 minutes
        breakout_ticks: float = 0.25, # one NQ tick beyond the range
        target_mult: float = 1.0,
        stop_mult: float = 0.5,
        deadline_bars: int = 15,      # 75 minutes after entry
        size: int = 1,
    ):
        self.interval = interval
        self.range_bars = range_bars
        self.breakout_ticks = breakout_ticks
        self.target_mult = target_mult
        self.stop_mult = stop_mult
        self.deadline_bars = deadline_bars
        self.size = size

        # Session state
        self._session = None
        self._bars_this_session = 0
        self._opening_high = None
        self._opening_low = None
        self._range_width = None
        self._entry_direction = None  # 1 for long, -1 for short, None before entry
        self._entry_price = None
        self._bars_held = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        # Reset per-session state on a new session
        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_this_session = 0
            self._opening_high = None
            self._opening_low = None
            self._range_width = None
            self._entry_direction = None
            self._entry_price = None
            self._bars_held = 0

        if not ctx.actionable:
            return

        # Manage an open position: deadline exit
        if ctx.position:
            self._bars_held += 1
            if self._bars_held >= self.deadline_bars:
                ctx.close(tag="deadline-exit")
            return

        # No position. If we have a pending entry order, do nothing (it rests).
        if ctx.working_order_ids:
            return

        self._bars_this_session += 1

        # Build the opening range from the first `range_bars` bars of the session
        if self._bars_this_session <= self.range_bars:
            history = ctx.history(self.interval)
            if len(history) < self._bars_this_session:
                return
            bar = history[-1]
            if self._opening_high is None or bar.high > self._opening_high:
                self._opening_high = bar.high
            if self._opening_low is None or bar.low < self._opening_low:
                self._opening_low = bar.low
            if self._bars_this_session == self.range_bars:
                if self._opening_high is not None and self._opening_low is not None:
                    self._range_width = self._opening_high - self._opening_low
                else:
                    self._range_width = None
                if self._range_width is not None and self._range_width <= 0:
                    self._range_width = None
            return

        # After the opening range is established, look for a breakout
        if self._range_width is None or self._range_width <= 0:
            return
        if self._opening_high is None or self._opening_low is None:
            return

        history = ctx.history(self.interval)
        if len(history) < self._bars_this_session:
            return
        bar = history[-1]

        # Breakout levels: one tick beyond the range
        long_trigger = self._opening_high + self.breakout_ticks
        short_trigger = self._opening_low - self.breakout_ticks

        if bar.close > self._opening_high + self.breakout_ticks:
            # Long breakout: place a stop entry at the trigger
            ctx.buy(
                size=self.size,
                type="stop",
                trigger=long_trigger,
                stop=self._opening_high - self.stop_mult * self._range_width,
                target=self._opening_high + self.target_mult * self._range_width,
                tif="DAY",
                tag="breakout-long",
            )
            self._entry_direction = 1
            self._entry_price = long_trigger
            self._bars_held = 0
        elif bar.close < self._opening_low - self.breakout_ticks:
            # Short breakout
            ctx.sell(
                size=self.size,
                type="stop",
                trigger=short_trigger,
                stop=self._opening_low + self.stop_mult * self._range_width,
                target=self._opening_low - self.target_mult * self._range_width,
                tif="DAY",
                tag="breakout-short",
            )
            self._entry_direction = -1
            self._entry_price = short_trigger
            self._bars_held = 0

    def on_fill(self, ctx) -> None:
        # The bracket handles stops and targets; we only track entry for deadline
        if ctx.fill.position != 0 and self._entry_direction is not None:
            self._entry_price = ctx.fill.price
            self._bars_held = 0

    def on_lifecycle(self, ctx) -> None:
        # If our entry order expires at session close, reset direction
        if ctx.event.kind in ("day_expired", "cancel_confirmed"):
            self._entry_direction = None
            self._entry_price = None
