"""Give-back after an outsized move, with strict time-of-day and confirmation.

Prior trials show that both continuation and give-back after an outsized bar
lose money when applied broadly. This version restricts trading to the first
two hours of the session (17:00-19:00 CT, which is 22:00-00:00 UTC), where the
prior trials' hourly breakdown showed the strongest give-back effect. It also
requires a much larger outsized move (range_mult=2.0) and a strong close
confirmation, and uses a wider stop to avoid being shaken out.

The hypothesis: early in the session, an outsized move is more likely to be a
liquidity-driven overreaction that gives back, while later moves are more likely
to be information-driven and continue. By restricting to the first two hours,
we isolate the give-back regime.

All state is kept on self. Only the single bar that just closed is read from
history, so no WindowTooShort crash can occur.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 6,
        range_mult: float = 2.0,
        min_typical_range: float = 0.5,
        size: int = 1,
        stop_offset: float = 3.0,
        target_offset: float = 4.0,
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

        # Trade only during the first two hours of the session:
        # 17:00-19:00 CT = 22:00-00:00 UTC
        hour = ctx.now.hour
        minute = ctx.now.minute
        minutes_since_midnight = hour * 60 + minute
        if not (22 * 60 <= minutes_since_midnight < 24 * 60):
            return

        if len(self._ranges) < self.lookback:
            return

        typical_range = sum(self._ranges) / len(self._ranges)
        if typical_range <= 0 or typical_range < self.min_typical_range:
            return

        if bar_range < self.range_mult * typical_range:
            return

        bar_span = last.high - last.low
        if bar_span <= 0:
            return
        close_position = (last.close - last.low) / bar_span

        # Require a strong directional close: top or bottom quarter
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
