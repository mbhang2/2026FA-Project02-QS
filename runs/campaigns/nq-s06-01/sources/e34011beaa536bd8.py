"""Opening-range breakout with direction chosen by the breakout itself.

The campaign question: when price leaves the opening range, does the move hold
or does the range win? This proposal answers "the move holds" and trades every
ordinary session that produces a clean breakout.

Mechanics:
- The opening range is the first 30 minutes of the regular-hours session,
  approximated with 5-minute bars (six bars).
- A breakout is a close strictly beyond the range high (long) or low (short).
- Enter on the bar close that breaks the range, with a protective bracket sized
  off the range height.
- One position per session; the bracket does the exit.

Why this shape:
- It acts on the ordinary run of sessions: a 30-minute range on NQ breaks on
  most sessions, so the rule fires often enough to be measured.
- Direction is left entirely to the breakout, not chosen after looking.
- The bracket is armed by the engine at the instant the entry fills, closing
  the gap between signal and protection.
"""

from __future__ import annotations


class Strategy:
    """Breakout of the first six 5-minute bars of each session."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        range_bars: int = 6,
        size: int = 1,
        stop_mult: float = 1.0,
        target_mult: float = 1.5,
    ) -> None:
        self.interval = interval
        self.range_bars = range_bars
        self.size = size
        self.stop_mult = stop_mult
        self.target_mult = target_mult

        self._session = -1
        self._bars_in_session = 0
        self._range_high = None
        self._range_low = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        # Track session and bar count within it.
        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_in_session = 0
            self._range_high = None
            self._range_low = None

        if "5m" not in ctx.bars:
            return

        self._bars_in_session += 1

        if not ctx.actionable:
            return
        if ctx.position or ctx.working_order_ids:
            return

        bar = ctx.bars["5m"]

        # Build the opening range from the first `range_bars` bars.
        if self._bars_in_session <= self.range_bars:
            if self._range_high is None or bar.high > self._range_high:
                self._range_high = bar.high
            if self._range_low is None or bar.low < self._range_low:
                self._range_low = bar.low
            return

        # The range is complete; wait for a close strictly beyond it.
        if self._range_high is None or self._range_low is None:
            return

        rng = self._range_high - self._range_low
        if rng <= 0:
            return

        stop = rng * self.stop_mult
        target = rng * self.target_mult

        if bar.close > self._range_high:
            ctx.buy(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="range-breakout-long",
            )
        elif bar.close < self._range_low:
            ctx.sell(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="range-breakout-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
