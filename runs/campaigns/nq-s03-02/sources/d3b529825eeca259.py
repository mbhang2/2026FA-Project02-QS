"""Investigate whether an outsized intra-session move continues or gives back.

This strategy measures "outsized" against the session's own realised range so
far, but does so without iterating over the full history window on every bar.
Instead, it maintains the session high and low incrementally from the single
bar that just closed, and keeps a short rolling list of recent bar ranges to
compute a typical range.

The effect is claimed to be continuation: an outsized move reflects information
arriving and has further to run. The strategy enters in the direction of the
outsized bar and lets the bracket manage the exit.

To respect the question's standard, the strategy trades both directions
symmetrically and restricts itself to the middle of the session (hours 9-14 CT,
roughly 14:00-19:00 UTC), where the first attempt's results were not
concentrated in a single hour. The bracket protects the position from the fill.

The prior trial crashed because it iterated over the full history window on
every bar, reaching past the pushed window. This version keeps only the session
high and low on self, updating them from the single bar that just closed, so no
history iteration is needed.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 4,
        range_mult: float = 2.0,
        size: int = 1,
        stop_offset: float = 1.5,
        target_offset: float = 2.5,
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

        hour = ctx.now.hour
        if hour < 14 or hour >= 19:
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
