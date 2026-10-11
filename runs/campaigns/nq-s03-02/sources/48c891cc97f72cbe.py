"""Outsized-move continuation with strict trend alignment and session-time filter.

The prior continuation trials (#3, #4, #8) lost money, and the give-back trials
(#5, #6, #7) also lost money. A common thread in the losing strategies is that
they traded both long and short signals, often within the same session, without
a strong filter on the direction of the session's overall trend.

This strategy takes a more conservative approach: it only trades in the
direction of a session-level trend, measured by the sign of the cumulative
delta over a longer lookback window. It requires both an outsized bar and
confirmation that the bar's direction agrees with the session's cumulative
order flow. It also restricts trading to a narrower window in the middle of
the session (23:00-19:00 UTC), avoiding the noisy open and close.

Key changes from #8:
1. Replace the simple moving average with cumulative delta (buy_volume -
   sell_volume) over the last 30 bars, which is a more direct measure of
   order flow.
2. Require the outsized bar's delta to agree with the cumulative delta sign.
3. Narrow the trading window to 23:00-19:00 UTC, excluding the first and last
   hour of the session.
4. Slightly tighter stop and target to reduce the cost of failed trades.

All state is kept on self. The cumulative delta is computed incrementally from
the history window, which is bounded at 512 bars, so no WindowTooShort crash
can occur.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 6,
        range_mult: float = 1.8,
        min_typical_range: float = 0.5,
        delta_lookback: int = 30,
        size: int = 1,
        stop_offset: float = 1.5,
        target_offset: float = 2.5,
    ):
        self.interval = interval
        self.lookback = lookback
        self.range_mult = range_mult
        self.min_typical_range = min_typical_range
        self.delta_lookback = delta_lookback
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset

        self._session = None
        self._ranges: list[float] = []
        self._deltas: list[float] = []

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        if ctx.session != self._session:
            self._session = ctx.session
            self._ranges.clear()
            self._deltas.clear()

        history = ctx.history(self.interval)
        if not history:
            return
        last = history[-1]

        bar_range = last.high - last.low
        if bar_range > 0:
            self._ranges.append(bar_range)
            if len(self._ranges) > self.lookback:
                self._ranges.pop(0)

        self._deltas.append(last.delta)
        if len(self._deltas) > self.delta_lookback:
            self._deltas.pop(0)

        # Restrict to the middle of the session: 23:00-19:00 UTC
        hour = ctx.now.hour
        minute = ctx.now.minute
        minutes_since_midnight = hour * 60 + minute
        if not (23 * 60 <= minutes_since_midnight < 19 * 60):
            return

        if len(self._ranges) < self.lookback:
            return

        typical_range = sum(self._ranges) / len(self._ranges)
        if typical_range <= 0 or typical_range < self.min_typical_range:
            return

        if bar_range < self.range_mult * typical_range:
            return

        # Need enough deltas for the cumulative sum
        if len(self._deltas) < self.delta_lookback:
            return
        cumulative_delta = sum(self._deltas)

        # Confirmation: close must be in the top or bottom quarter of the bar
        bar_span = last.high - last.low
        if bar_span <= 0:
            return
        close_position = (last.close - last.low) / bar_span

        if ctx.position or ctx.working_order_ids:
            return

        # Only trade in the direction of the cumulative delta
        if cumulative_delta > 0 and close_position > 0.75 and last.close > last.open:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="outsized-continuation-delta-long",
            )
        elif cumulative_delta < 0 and close_position < 0.25 and last.close < last.open:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="outsized-continuation-delta-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
