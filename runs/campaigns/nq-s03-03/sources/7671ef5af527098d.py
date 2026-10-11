"""Investigate whether an outsized intra-session move tends to continue or give back.

This strategy tests a **filtered continuation** hypothesis: an outsized bar
(range >= multiple of rolling ATR) followed by a bar that closes beyond the
outsized bar's extreme, but only when the outsized bar itself closed strongly in
its direction (close in the top/bottom quartile of its range). The idea is that
an outsized move with a weak close is more likely to be a spike that gives back,
while one with a strong close represents genuine directional pressure that
continues.

The mechanism is claimed to operate throughout the session, independent of hour,
month, or weekday.

Key design choices:
- Rolling ATR (default 20 bars) adapts to current volatility.
- Outsized bar: range >= range_mult * ATR (default 2.0).
- Strong close filter: close must be in the top 25% of the bar's range for an up
  move, or bottom 25% for a down move.
- Continuation confirmation: next bar closes beyond the outsized bar's extreme
  by at least continuation_frac * outsized range (default 0.1).
- Bracket exits use ATR-scaled offsets.
"""

from __future__ import annotations


class Strategy:
    """Trade continuation of outsized moves with a strong-close filter."""

    def __init__(self, *, interval: str = "5m", atr_window: int = 20,
                 range_mult: float = 2.0, continuation_frac: float = 0.1,
                 strong_close_frac: float = 0.25,
                 stop_atr: float = 1.0, target_atr: float = 2.0,
                 size: int = 1):
        self.interval = interval
        self.atr_window = atr_window
        self.range_mult = range_mult
        self.continuation_frac = continuation_frac
        self.strong_close_frac = strong_close_frac
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

        # Strong-close filter: for an up move, close must be in the top
        # strong_close_frac of the bar's range; for a down move, in the bottom.
        if prev_direction > 0:
            close_position = (prev_bar.close - prev_bar.low) / prev_range
            if close_position < 1.0 - self.strong_close_frac:
                return
            required_break = prev_bar.high + self.continuation_frac * prev_range
            if bar.close <= required_break:
                return
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_atr * atr,
                target_offset=self.target_atr * atr,
                tag="filtered-continuation-long",
            )
        else:
            close_position = (prev_bar.high - prev_bar.close) / prev_range
            if close_position < 1.0 - self.strong_close_frac:
                return
            required_break = prev_bar.low - self.continuation_frac * prev_range
            if bar.close >= required_break:
                return
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_atr * atr,
                target_offset=self.target_atr * atr,
                tag="filtered-continuation-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
