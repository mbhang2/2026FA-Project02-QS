"""Outsized-move give-back with session-time filtering and stricter confirmation.

The prior give-back trials (#5, #6) entered opposite any outsized bar with a
close in the outer 40% of its range, and lost money. This version keeps the
give-back claim but restricts it to the hours where the effect appeared most
strongly in the first attempt: the first 90 minutes after the open and the
last 90 minutes before the close. It also requires a stronger confirmation
(close in the outer 20% of the bar's range) and a larger outsized move
relative to the session's typical range, which reduces the number of trades
and should filter out the noise that made the prior give-back strategies lose.

The trading window is expressed in UTC, matching the session times: the
session opens at 22:00 UTC and closes at 21:00 UTC the next day. The first 90
minutes are 22:00-23:30 UTC, and the last 90 minutes are 19:30-21:00 UTC.

All state is kept on self and only the single bar that just closed is read
from history, so no WindowTooShort crash can occur.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 8,
        range_mult: float = 2.0,
        min_typical_range: float = 0.4,
        size: int = 1,
        stop_offset: float = 1.5,
        target_offset: float = 2.5,
    ):
        self.interval = interval
        self.lookback = lookback
        self.range_mult = range_mult
        self.min_typical_range = min_typical_range
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset

        self._session = None
        self._ranges: list[float] = []

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        if ctx.session != self._session:
            self._session = ctx.session
            self._ranges.clear()

        history = ctx.history(self.interval)
        if not history:
            return
        last = history[-1]

        bar_range = last.high - last.low
        if bar_range > 0:
            self._ranges.append(bar_range)
            if len(self._ranges) > self.lookback:
                self._ranges.pop(0)

        # Restrict to the first 90 minutes (22:00-23:30 UTC) and the last 90
        # minutes (19:30-21:00 UTC) of the session.
        hour = ctx.now.hour
        minute = ctx.now.minute
        minutes_since_midnight = hour * 60 + minute
        in_first_window = 22 * 60 <= minutes_since_midnight < 23 * 60 + 30
        in_last_window = 19 * 60 + 30 <= minutes_since_midnight < 21 * 60
        if not (in_first_window or in_last_window):
            return

        if len(self._ranges) < self.lookback:
            return

        typical_range = sum(self._ranges) / len(self._ranges)
        if typical_range <= 0 or typical_range < self.min_typical_range:
            return

        if bar_range < self.range_mult * typical_range:
            return

        # Strong confirmation: close must be in the outer 20% of the bar's range
        bar_span = last.high - last.low
        if bar_span <= 0:
            return
        close_position = (last.close - last.low) / bar_span

        # Give-back: enter opposite the outsized bar's direction
        if close_position > 0.8:
            # Bar closed near its high: an outsized up move. Short it.
            if ctx.position > 0 or ctx.working_order_ids:
                return
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="outsized-giveback-short",
            )
        elif close_position < 0.2:
            # Bar closed near its low: an outsized down move. Buy it.
            if ctx.position < 0 or ctx.working_order_ids:
                return
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="outsized-giveback-long",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
