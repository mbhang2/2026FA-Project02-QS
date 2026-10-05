"""Opening-range fade: when price leaves the opening range, the range wins.

The campaign question asks whether the move holds or the range wins. Prior
trials answered "the move holds" with breakout entries. This proposal answers
"the range wins": it fades the breakout, entering in the opposite direction
when a bar closes beyond the opening range.

Mechanics:
- Opening range is the first 30 minutes of the regular-hours session, built
  from 5-minute bars (six bars).
- After the range is established, a close above the range high triggers a
  short entry; a close below the range low triggers a long entry.
- Bracket sized from the opening range width: stop at 1.0x range, target at
  0.75x range, so the trade captures the reversion without giving back too much.
- One position per session; the bracket handles exits.

Why this shape:
- It acts on the ordinary run of sessions, because the opening range is broken
  on most sessions and the fade fires on every breakout.
- Direction is strictly the opposite of the breakout, not chosen after looking.
- The bracket is armed by the engine at the instant the entry fills.
"""

from __future__ import annotations


class Strategy:
    """Fade the opening-range breakout."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        range_bars: int = 6,
        size: int = 1,
        stop_mult: float = 1.0,
        target_mult: float = 0.75,
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

        if self.interval not in ctx.bars:
            return

        self._bars_in_session += 1

        if not ctx.actionable:
            return
        if ctx.position or ctx.working_order_ids:
            return

        bar = ctx.bars[self.interval]

        # Build the opening range from the first `range_bars` bars.
        if self._bars_in_session <= self.range_bars:
            if self._range_high is None or bar.high > self._range_high:
                self._range_high = bar.high
            if self._range_low is None or bar.low < self._range_low:
                self._range_low = bar.low
            return

        if self._range_high is None or self._range_low is None:
            return

        rng = self._range_high - self._range_low
        if rng <= 0:
            return

        stop = rng * self.stop_mult
        target = rng * self.target_mult

        # Fade the breakout: close above range high -> short, below low -> long.
        if bar.close > self._range_high:
            ctx.sell(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="range-fade-short",
            )
        elif bar.close < self._range_low:
            ctx.buy(
                size=self.size,
                stop_offset=stop,
                target_offset=target,
                tag="range-fade-long",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
