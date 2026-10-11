"""Give-back after an outsized move, with session-relative trigger and no rolling window.

Prior trials show that both continuation and give-back lose money when applied
broadly. The best score so far is -0.2898 (#4), which used a rolling window of
recent bar ranges and continuation. This version takes a different approach:

- Measures the outsized move relative to the session's own realized range so
  far, using all bars in the session rather than a short rolling window. This
  makes the trigger more stable and less prone to being fired by a single
  quiet bar.
- Trades the give-back direction (outsized up bar -> short; outsized down bar
  -> long), which prior trials have not tested with this session-relative
  trigger.
- Requires a strong directional close (top/bottom 25% of the bar's range).
- Restricts trading to the middle of the session (18:00-21:00 CT = 23:00-02:00
  UTC), avoiding the open and close where the first attempt's results were
  most erratic.
- Uses a wider stop (3.0) and target (4.0) to give the reversion room.

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

        # Require a strong directional close: top or bottom 25%
        if not (close_position > 0.75 or close_position < 0.25):
            return

        if ctx.position or ctx.working_order_ids:
            return

        # Give-back: outsized up bar -> short; outsized down bar -> long
        if close_position > 0.75 and last.close > last.open:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="outsized-giveback-short",
            )
        elif close_position < 0.25 and last.close < last.open:
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
