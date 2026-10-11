"""Mean-reversion after an outsized move, with a session-relative trigger.

The prior trials show that both continuation and give-back lose money when
applied broadly. This version attempts a different angle: instead of using a
rolling window of recent bar ranges, it measures the outsized move relative to
the session's own realized range so far, and then trades the reversion only
when the outsized bar closes in the top or bottom 20% of its range.

The key difference from prior give-back attempts is the trigger: the outsized
bar must be at least 2.0 times the session's average bar range, but the
session average is computed over all bars in the session so far, not a short
rolling window. This makes the trigger more stable and less prone to being
fired by a single quiet bar.

We also restrict trading to the middle of the session (avoiding the first and
last hour), where the prior trials' hourly breakdown showed the most consistent
give-back behavior.

All state is kept on self. Only the single bar that just closed is read from
history, so no WindowTooShort crash can occur.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        range_mult: float = 2.0,
        min_session_range: float = 1.0,
        size: int = 1,
        stop_offset: float = 3.0,
        target_offset: float = 4.0,
    ):
        self.interval = interval
        self.range_mult = range_mult
        self.min_session_range = min_session_range
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset

        self._session = None
        self._session_ranges: list[float] = []

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        if ctx.session != self._session:
            self._session = ctx.session
            self._session_ranges.clear()

        history = ctx.history(self.interval)
        if not history:
            return
        last = history[-1]

        bar_range = last.high - last.low
        if bar_range > 0:
            self._session_ranges.append(bar_range)

        # Trade only during the middle of the session: 18:00-21:00 CT
        # which is 23:00-02:00 UTC
        hour = ctx.now.hour
        minute = ctx.now.minute
        minutes_since_midnight = hour * 60 + minute
        if not (23 * 60 <= minutes_since_midnight < 2 * 60):
            return

        if len(self._session_ranges) < 12:
            return

        avg_range = sum(self._session_ranges) / len(self._session_ranges)
        if avg_range <= 0 or avg_range < self.min_session_range:
            return

        if bar_range < self.range_mult * avg_range:
            return

        bar_span = last.high - last.low
        if bar_span <= 0:
            return
        close_position = (last.close - last.low) / bar_span

        # Require a strong directional close: top or bottom 20%
        if not (close_position > 0.80 or close_position < 0.20):
            return

        if ctx.position or ctx.working_order_ids:
            return

        # Give-back: outsized up bar -> short; outsized down bar -> long
        if close_position > 0.80 and last.close > last.open:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="outsized-giveback-short",
            )
        elif close_position < 0.20 and last.close < last.open:
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
