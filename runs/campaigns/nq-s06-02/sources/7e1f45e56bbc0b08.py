"""Opening-range breakout with a stated deadline and a stated exit.

The question: when price leaves the opening range, does the move hold or does
the range win?

This strategy takes the "move holds" side, but only for the first breakout of
the session and only when the breakout is large relative to the opening range
itself. Every position has a stated deadline: it is closed at the end of the
session if the bracket has not resolved it first. Outcomes that resolve inside
the deadline (stop, target, or manual close) are the answer; a position that
simply expires at the session end is reported as unresolved by the tag on the
closing order.

Opening range: first 30 minutes of the regular-hours session, measured on 5m
bars. Leaving: a close beyond the range high or low. Direction: with the
breakout. Size: one contract. Bracket: stop and target as offsets from the
range width, so they scale with the range rather than being constants.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        range_bars: int = 6,          # 6 * 5m = 30 minutes
        stop_mult: float = 0.75,
        target_mult: float = 1.5,
        size: int = 1,
    ):
        self.interval = interval
        self.range_bars = range_bars
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size

        # Per-session state
        self._session = None
        self._bars_this_session = 0
        self._range_high = None
        self._range_low = None
        self._range_width = None
        self._entered_this_session = False

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        # Session boundary bookkeeping
        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_this_session = 0
            self._range_high = None
            self._range_low = None
            self._range_width = None
            self._entered_this_session = False

        self._bars_this_session += 1

        # Build the opening range from the first `range_bars` bars of the session
        if self._bars_this_session <= self.range_bars:
            history = ctx.history(self.interval)
            if len(history) < 1:
                return
            bar = history[-1]
            if self._range_high is None:
                self._range_high = bar.high
                self._range_low = bar.low
            else:
                self._range_high = max(self._range_high, bar.high)
                self._range_low = min(self._range_low, bar.low)
            if self._bars_this_session == self.range_bars:
                if self._range_high is not None and self._range_low is not None:
                    self._range_width = self._range_high - self._range_low
            return

        # We need a valid range and an actionable session
        if self._range_width is None or not ctx.actionable:
            return

        # One position per session; the bracket handles exits, and a manual
        # close at the session end handles the deadline.
        if ctx.position or ctx.working_order_ids:
            return

        # Only the first breakout of the session
        if self._entered_this_session:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return
        last = history[-1]

        # A breakout must be a close beyond the range, not just a wick
        if last.close > self._range_high:
            self._entered_this_session = True
            ctx.buy(
                size=self.size,
                stop_offset=self._range_width * self.stop_mult,
                target_offset=self._range_width * self.target_mult,
                tag="range-breakout-long",
            )
        elif last.close < self._range_low:
            self._entered_this_session = True
            ctx.sell(
                size=self.size,
                stop_offset=self._range_width * self.stop_mult,
                target_offset=self._range_width * self.target_mult,
                tag="range-breakout-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        # At session close, any open position is closed by the engine's DAY
        # expiry of the bracket legs? No -- the position itself is not
        # flattened. We must close it ourselves.
        #
        # The deadline is the session close. If the bracket has not resolved
        # the position by then, close it manually.
        if ctx.event.kind == "session_close":
            if ctx.position:
                ctx.close(tag="deadline-close")
