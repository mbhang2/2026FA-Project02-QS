"""Investigate whether an outsized move inside a session continues.

Hypothesis: after a bar whose range is large relative to the session's
average bar range so far, price continues in the direction of that bar.
We measure "outsized" as the current bar's range exceeding a multiple of
the average range of bars already seen in the session. When such a bar
occurs, we enter in the direction of its close relative to open, with a
bracket sized off the bar's range. We require a minimum number of bars
to establish a baseline and a minimum time left in the session to avoid
holding into the close.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        min_bars: int = 10,
        range_mult: float = 2.0,
        stop_mult: float = 1.0,
        target_mult: float = 1.5,
        size: int = 1,
        min_time_left_ns: int = 30 * 60 * 1_000_000_000,  # 30 minutes
    ):
        self.interval = interval
        self.min_bars = min_bars
        self.range_mult = range_mult
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size
        self.min_time_left_ns = min_time_left_ns

        # Session state
        self._session = None
        self._bars_this_session = 0
        self._sum_ranges = 0.0
        self._session_high = None
        self._session_low = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        # Reset session state on session change
        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_this_session = 0
            self._sum_ranges = 0.0
            self._session_high = None
            self._session_low = None

        # Only act on our interval's bar close
        if self.interval not in ctx.bars:
            return

        bar = ctx.bars[self.interval]
        self._bars_this_session += 1

        # Update session high/low
        if self._session_high is None or bar.high > self._session_high:
            self._session_high = bar.high
        if self._session_low is None or bar.low < self._session_low:
            self._session_low = bar.low

        bar_range = bar.high - bar.low
        self._sum_ranges += bar_range

        # Need enough bars to compute a baseline average range
        if self._bars_this_session < self.min_bars:
            return

        # Check time left in session
        time_left = ctx.session_end - ctx.ts
        if time_left < self.min_time_left_ns:
            return

        # Only one position at a time
        if ctx.position or ctx.working_order_ids:
            return

        # Compute average range of bars so far (excluding current? we include it)
        avg_range = self._sum_ranges / self._bars_this_session
        if avg_range <= 0:
            return

        # Outsized move if current bar range exceeds multiple of average
        if bar_range < self.range_mult * avg_range:
            return

        # Direction: continuation (close > open -> buy, close < open -> sell)
        if bar.close == bar.open:
            return

        # Bracket offsets based on bar range
        stop_offset = max(bar_range * self.stop_mult, 0.25)
        target_offset = max(bar_range * self.target_mult, 0.25)

        if bar.close > bar.open:
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="outsized-continuation-long",
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="outsized-continuation-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
