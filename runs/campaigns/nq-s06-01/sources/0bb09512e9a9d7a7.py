"""Opening-range breakout with prior-session bias, but fading the breakout.

The campaign question: when price leaves the opening range, does the move hold
or does the range win? This strategy answers "the range wins": it fades
breakouts in the direction opposite the prior session's bias.

Mechanics:
- Opening range = first 30 minutes of the session, measured on 5-minute bars.
- A breakout is a 5-minute bar that closes beyond the opening range high or low.
- Direction filter: prior session's daily bar must be DOWN for us to fade an
  upside breakout (sell), or UP for us to fade a downside breakout (buy).
  In other words, we take the opposite side of the breakout when the prior
  session's direction suggests the breakout is overextended.
- Entry on the close of the breakout bar, with a protective bracket sized from
  the opening range width.

This acts on most sessions: the only filter is the prior-session direction, and
a flat prior session simply skips that session.
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

        # Fade the breakout: if the prior session was up, we sell upside
        # breakouts; if the prior session was down, we buy downside breakouts.
        if bar.close > self._opening_high and bias < 0:
            stop = self.stop_mult * self._opening_range
            target = self.target_mult * self._opening_range
            ctx.sell(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="fade-long-breakout",
            )
        elif bar.close < self._opening_low and bias > 0:
            stop = self.stop_mult * self._opening_range
            target = self.target_mult * self._opening_range
            ctx.buy(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="fade-short-breakout",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
