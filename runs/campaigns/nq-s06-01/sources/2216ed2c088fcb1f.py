"""Opening-range breakout with a directional bias from the prior session.

The campaign question: when price leaves the opening range, does the move hold
or does the range win? This strategy answers "the move holds" in the direction
of the prior session's bias, on the ordinary run of sessions.

Mechanics:
- The opening range is the first 30 minutes of the regular-hours session,
  measured on 5-minute bars.
- A breakout is a 5-minute bar that closes beyond the opening range high or low.
- Direction filter: the prior session's daily bar must agree -- up prior session
  means only long breakouts, down prior session means only short breakouts.
- Entry is on the close of the breakout bar, with a protective bracket sized
  from the opening range width.

This acts on most sessions: the only filter is the prior-session direction, and
a flat prior session simply skips that session rather than gating on multiple
conditions.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        daily_interval: str = "1d",
        opening_minutes: int = 30,
        size: int = 1,
        stop_mult: float = 0.5,
        target_mult: float = 1.5,
    ):
        self.interval = interval
        self.daily_interval = daily_interval
        self.opening_minutes = opening_minutes
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult

        # State per session
        self._session = None
        self._opening_high = None
        self._opening_low = None
        self._opening_range = None
        self._session_bars = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)
        ctx.subscribe(self.daily_interval)

    def on_bars(self, ctx) -> None:
        # Daily bar closes at session end -- not a decision point for entries.
        if self.interval not in ctx.bars:
            return

        if not ctx.actionable:
            return

        # One position at a time; the bracket handles exits.
        if ctx.position or ctx.working_order_ids:
            return

        # Session tracking
        if ctx.session != self._session:
            self._session = ctx.session
            self._opening_high = None
            self._opening_low = None
            self._opening_range = None
            self._session_bars = 0

        self._session_bars += 1

        # Build the opening range from the first `opening_minutes` of bars.
        bar = ctx.bars[self.interval]
        opening_bars = self.opening_minutes // 5
        if self._session_bars <= opening_bars:
            if self._opening_high is None or bar.high > self._opening_high:
                self._opening_high = bar.high
            if self._opening_low is None or bar.low < self._opening_low:
                self._opening_low = bar.low
            if self._session_bars == opening_bars:
                if self._opening_high is not None and self._opening_low is not None:
                    self._opening_range = self._opening_high - self._opening_low
            return  # still building the range

        if self._opening_range is None or self._opening_range <= 0:
            return

        # Prior session daily bar for direction.
        daily = ctx.history(self.daily_interval)
        if len(daily) < 2:
            return  # need at least one completed prior session
        prev = daily[-2]
        bias = prev.close - prev.open
        if bias == 0:
            return

        # Breakout detection on the current bar's close.
        if bar.close > self._opening_high and bias > 0:
            stop = self.stop_mult * self._opening_range
            target = self.target_mult * self._opening_range
            ctx.buy(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="breakout-long",
            )
        elif bar.close < self._opening_low and bias < 0:
            stop = self.stop_mult * self._opening_range
            target = self.target_mult * self._opening_range
            ctx.sell(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="breakout-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
