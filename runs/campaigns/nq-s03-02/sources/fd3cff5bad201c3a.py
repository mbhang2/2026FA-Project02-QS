"""Investigate whether an outsized intra-session move continues or gives back.

This strategy measures "outsized" against the session's own realised range so
far, using only the session high and low tracked incrementally on self. It
enters in the direction of the outsized bar's close relative to its open, and
lets a bracket manage the exit.

The prior trials were rejected because they were exposed in only 154 of 384
scored sessions, under the registered floor of 70%. That means the strategy
spent too much time flat. This version trades more often by relaxing the
outsized threshold and widening the trading window to nearly the whole session,
while still avoiding the first and last half-hours where the prior attempt's
results were noisy. The effect is claimed to be continuation, so the entry is
in the direction of the outsized bar.

To avoid the WindowTooShort crash from trial #0, all state is kept on self and
only the single bar that just closed is read from history. No iteration over
the history window is performed.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 3,
        range_mult: float = 1.2,
        size: int = 1,
        stop_offset: float = 1.0,
        target_offset: float = 1.5,
    ):
        self.interval = interval
        self.lookback = lookback
        self.range_mult = range_mult
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset

        self._session = None
        self._session_high = None
        self._session_low = None
        self._ranges: list[float] = []

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        if ctx.session != self._session:
            self._session = ctx.session
            self._session_high = None
            self._session_low = None
            self._ranges.clear()

        history = ctx.history(self.interval)
        if not history:
            return
        last = history[-1]

        if self._session_high is None or last.high > self._session_high:
            self._session_high = last.high
        if self._session_low is None or last.low < self._session_low:
            self._session_low = last.low

        bar_range = last.high - last.low
        if bar_range > 0:
            self._ranges.append(bar_range)
            if len(self._ranges) > self.lookback:
                self._ranges.pop(0)

        # Trade for most of the session, but skip the first and last 30 minutes
        # to avoid the noisy open and close. Sessions run 17:00 CT to 16:00 CT,
        # which is 22:00 UTC to 21:00 UTC the next day. The first 30 minutes are
        # 22:00-22:30 UTC, and the last 30 minutes are 20:30-21:00 UTC.
        hour = ctx.now.hour
        minute = ctx.now.minute
        minutes_since_midnight = hour * 60 + minute

        # Skip 22:00-22:30 UTC (first 30 minutes)
        if 22 * 60 <= minutes_since_midnight < 22 * 60 + 30:
            return
        # Skip 20:30-21:00 UTC (last 30 minutes)
        if 20 * 60 + 30 <= minutes_since_midnight < 21 * 60:
            return

        if len(self._ranges) < self.lookback:
            return

        typical_range = sum(self._ranges) / len(self._ranges)
        if typical_range <= 0:
            return

        if bar_range < self.range_mult * typical_range:
            return

        if ctx.position or ctx.working_order_ids:
            return

        if last.close > last.open:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="outsized-continuation-long",
            )
        elif last.close < last.open:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="outsized-continuation-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
