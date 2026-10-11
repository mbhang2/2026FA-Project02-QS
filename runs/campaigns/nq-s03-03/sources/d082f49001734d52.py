"""Investigate whether an outsized intra-session move tends to continue or give back.

This strategy tests the **continuation** hypothesis, the opposite of the prior
give-back attempts. Instead of fading an outsized move, we trade in the direction
of the move after it has shown persistence: an outsized bar (range > multiple of
rolling ATR) followed by a bar that closes further in the same direction beyond
the outsized bar's extreme.

The mechanism is claimed to operate throughout the session: information-driven
moves should continue regardless of hour, month, or weekday. The strategy does
not condition on any of these.

Key design choices:
- Rolling ATR (default 20 bars) adapts to current volatility.
- Outsized bar: range >= range_mult * ATR (default 2.0).
- Continuation confirmation: the current bar's close must exceed the outsized
  bar's high (for up moves) or fall below its low (for down moves) by at least
  a fraction of the outsized bar's range (default 0.2). This filters for
  genuine continuation rather than mere drift.
- Bracket exits use ATR-scaled offsets, keeping risk proportional to current
  volatility.
"""

from __future__ import annotations


class Strategy:
    """Trade continuation of outsized moves with rolling ATR and breakout confirmation."""

    def __init__(self, *, interval: str = "5m", atr_window: int = 20,
                 range_mult: float = 2.0, continuation_frac: float = 0.2,
                 stop_atr: float = 1.0, target_atr: float = 2.0,
                 size: int = 1):
        self.interval = interval
        self.atr_window = atr_window
        self.range_mult = range_mult
        self.continuation_frac = continuation_frac
        self.stop_atr = stop_atr
        self.target_atr = target_atr
        self.size = size

        # Per-session state.
        self._session = None
        self._trs: list[float] = []
        self._prev_close = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Reset state when the session changes.
        if ctx.session != self._session:
            self._session = ctx.session
            self._trs = []
            self._prev_close = None

        history = ctx.history(self.interval)
        if len(history) < 3:
            return

        bar = history[-1]

        # Update true range series.
        if self._prev_close is None:
            self._prev_close = bar.open
        tr = max(bar.high - bar.low,
                 abs(bar.high - self._prev_close),
                 abs(bar.low - self._prev_close))
        self._trs.append(tr)
        self._prev_close = bar.close

        # Need at least a few bars for a meaningful ATR.
        if len(self._trs) < 3:
            return

        # Rolling ATR over the most recent bars.
        window = self._trs[-self.atr_window:]
        atr = sum(window) / len(window)
        if atr <= 0:
            return

        # One position at a time; the bracket handles exits.
        if ctx.position or ctx.working_order_ids:
            return

        prev_bar = history[-2]
        prev_range = prev_bar.high - prev_bar.low
        if prev_range < self.range_mult * atr:
            return

        # Direction of the outsized move: close vs open.
        prev_direction = 1 if prev_bar.close > prev_bar.open else -1

        # Continuation requires the current bar to break the outsized bar's
        # extreme by at least continuation_frac of the outsized range.
        if prev_direction > 0:
            # Outsized up move: continuation means close above prev_bar.high
            # by at least continuation_frac * prev_range.
            required_break = prev_bar.high + self.continuation_frac * prev_range
            if bar.close <= required_break:
                return
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_atr * atr,
                target_offset=self.target_atr * atr,
                tag="continuation-long",
            )
        else:
            # Outsized down move: continuation means close below prev_bar.low
            # by at least continuation_frac * prev_range.
            required_break = prev_bar.low - self.continuation_frac * prev_range
            if bar.close >= required_break:
                return
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_atr * atr,
                target_offset=self.target_atr * atr,
                tag="continuation-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
