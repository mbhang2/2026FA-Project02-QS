"""Opening-range breakout strategy: does the move hold when price leaves the
opening range?

The opening range is the high and low of the first `range_minutes` of the
regular-hours session. We take a breakout in the direction of the move when a
bar closes beyond the range, and let a bracket manage the exit. The question
asks whether the move holds; this strategy answers by buying strength and
selling weakness at the boundary.

The range is built from one-minute bars, which is fine enough to capture the
opening high/low without a tick subscription. We subscribe to 1m for the range
and 5m for the decision bars. On each 5m close, if the range is not yet
established we do nothing. Once it is, a close above the range high enters
long; a close below the range low enters short. The bracket gives the move
room to hold and protects against the range winning.

Why this shape: the campaign says the direction is ours to choose, and the
simplest answer to "does the move hold" is to trade in the direction of the
breakout. No multi-stage confirmation, no regime switching. It acts on most
sessions because the opening range is usually broken at least once.
"""

from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        range_minutes: int = 30,
        decision_interval: str = "5m",
        range_interval: str = "1m",
        size: int = 1,
        stop_offset: float = 2.0,
        target_offset: float = 3.0,
    ):
        self.range_minutes = range_minutes
        self.decision_interval = decision_interval
        self.range_interval = range_interval
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset

        # Per-session state
        self._session = None
        self._range_high = None
        self._range_low = None
        self._range_bars_seen = 0
        self._range_established = False

    def setup(self, ctx) -> None:
        ctx.subscribe(self.decision_interval)
        ctx.subscribe(self.range_interval)

    def on_bars(self, ctx) -> None:
        if ctx.session != self._session:
            self._session = ctx.session
            self._range_high = None
            self._range_low = None
            self._range_bars_seen = 0
            self._range_established = False

        # Update the opening range from 1m bars until it is established.
        if not self._range_established and self.range_interval in ctx.bars:
            bar = ctx.bars[self.range_interval]
            self._range_bars_seen += 1
            if self._range_high is None or bar.high > self._range_high:
                self._range_high = bar.high
            if self._range_low is None or bar.low < self._range_low:
                self._range_low = bar.low
            if self._range_bars_seen >= self.range_minutes:
                self._range_established = True

        # Act only on decision bars, and only once the range exists.
        if self.decision_interval not in ctx.bars:
            return
        if not self._range_established:
            return
        if not ctx.actionable:
            return
        if ctx.position or ctx.working_order_ids:
            return

        bar = ctx.bars[self.decision_interval]
        if bar.close > self._range_high:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="breakout-long",
            )
        elif bar.close < self._range_low:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="breakout-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
