"""Opening-range breakout with a holding-period filter.

The question: when price leaves the opening range, does the move hold or does
the range win? This strategy answers "the move holds" for a specific, ordinary
definition of the opening range, and it acts on nearly every session.

Design:
- The opening range is the first 30 minutes of the regular-hours session,
  measured on 5-minute bars.
- "Leaving" the range is a close strictly above the range high (for a long)
  or strictly below the range low (for a short), in a bar that starts after
  the range has closed.
- Entry is a market order on the bar after the breakout bar. This keeps the
  rule simple and acts on the ordinary run of sessions.
- A bracket protects the position from the instant the entry fills. The stop
  is placed at the opposite side of the opening range; the target is a
  multiple of the range height.
- One position at a time. If the bracket is working, no new entry is taken.
- The rule conditions on the opening range by construction: without the
  range there is no signal. It acts on almost every session, because the
  opening range is broken on nearly all of them.
"""
from __future__ import annotations


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        range_bars: int = 6,          # 6 x 5m = 30 minutes
        target_mult: float = 1.5,
        size: int = 1,
    ):
        self.interval = interval
        self.range_bars = range_bars
        self.target_mult = target_mult
        self.size = size

        self._session = None
        self._bar_count = 0
        self._range_high = None
        self._range_low = None
        self._breakout_direction = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        # Reset per-session state when a new session begins.
        if ctx.session != self._session:
            self._session = ctx.session
            self._bar_count = 0
            self._range_high = None
            self._range_low = None
            self._breakout_direction = None

        self._bar_count += 1

        # Build the opening range from the first `range_bars` bars.
        if self._bar_count <= self.range_bars:
            bar = ctx.history(self.interval)[-1]
            if self._range_high is None:
                self._range_high = bar.high
                self._range_low = bar.low
            else:
                self._range_high = max(self._range_high, bar.high)
                self._range_low = min(self._range_low, bar.low)
            return

        if not ctx.actionable:
            return

        if ctx.position or ctx.working_order_ids:
            return

        if self._range_high is None or self._range_low is None:
            return

        bar = ctx.history(self.interval)[-1]

        # A breakout is a close beyond the range. We act on the next bar,
        # so the signal is already one bar old and no lookahead is involved.
        if self._breakout_direction is None:
            if bar.close > self._range_high:
                self._breakout_direction = "long"
            elif bar.close < self._range_low:
                self._breakout_direction = "short"
            return

        # This is the bar after the breakout bar: place the entry.
        if self._breakout_direction == "long":
            stop = self._range_low
            target = self._range_high + self.target_mult * (
                self._range_high - self._range_low
            )
            ctx.buy(
                size=self.size,
                stop=stop,
                target=target,
                tag="opening-range-long",
            )
        else:
            stop = self._range_high
            target = self._range_low - self.target_mult * (
                self._range_high - self._range_low
            )
            ctx.sell(
                size=self.size,
                stop=stop,
                target=target,
                tag="opening-range-short",
            )

        # Reset so we do not re-enter on later bars of the same session.
        self._breakout_direction = None

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
