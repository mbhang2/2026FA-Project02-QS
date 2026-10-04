"""Does an outsized move inside a session continue, or give it back?

This strategy measures "large" as a move that is large relative to the
session's own realized range so far, using the session's high-low range as
the yardstick. When the price extends the session range by a meaningful
fraction beyond what the session has already established, we take a position
in the direction of the extension and hold briefly with a protective stop.

The core idea: a break of the session's established range, when the range
itself is already non-trivial, is a continuation signal. We enter on the
close of the bar that makes the new extreme, with a stop that gives the
position room but not unlimited room, and a target that captures the
continuation without waiting for the session close.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        breakout_frac: float = 0.15,
        min_range: float = 2.0,
        stop_offset: float = 1.0,
        target_offset: float = 2.0,
        size: int = 1,
    ):
        self.interval = interval
        self.breakout_frac = breakout_frac
        self.min_range = min_range
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self.size = size

        # Per-session state
        self._session = None
        self._session_high = None
        self._session_low = None
        self._session_range = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        # Reset session tracking when a new session starts
        if ctx.session != self._session:
            self._session = ctx.session
            self._session_high = None
            self._session_low = None
            self._session_range = None

        if not ctx.actionable:
            return

        if ctx.position or ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        bar = history[-1]  # the bar that just closed

        # Update session high/low with the closed bar
        if self._session_high is None or bar.high > self._session_high:
            self._session_high = bar.high
        if self._session_low is None or bar.low < self._session_low:
            self._session_low = bar.low

        # Need a meaningful session range before a breakout means anything
        if self._session_high is None or self._session_low is None:
            return
        current_range = self._session_high - self._session_low
        if current_range < self.min_range:
            return

        # The bar that just closed must have made a new session extreme
        # AND the extension must be a meaningful fraction of the prior range
        extension = self.breakout_frac * current_range

        if bar.high >= self._session_high and bar.close > bar.open:
            # New high and the bar closed up: continuation long
            prior_high = self._session_high - (bar.high - self._session_high)
            if prior_high is None:
                return
            # The bar extended the high by more than the threshold
            if bar.high - prior_high >= extension:
                ctx.buy(
                    size=self.size,
                    stop_offset=self.stop_offset,
                    target_offset=self.target_offset,
                    tag="breakout-long",
                )
        elif bar.low <= self._session_low and bar.close < bar.open:
            # New low and the bar closed down: continuation short
            prior_low = self._session_low + (self._session_low - bar.low)
            if prior_low is None:
                return
            if prior_low - bar.low >= extension:
                ctx.sell(
                    size=self.size,
                    stop_offset=self.stop_offset,
                    target_offset=self.target_offset,
                    tag="breakout-short",
                )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
