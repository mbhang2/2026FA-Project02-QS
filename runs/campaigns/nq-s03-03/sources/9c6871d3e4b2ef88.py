"""Investigate whether an outsized intra-session move tends to continue or give back.

This strategy tests the continuation hypothesis in a refined form: an outsized
move is defined as a bar whose range exceeds a multiple of the session's average
true range so far, and entry is in the direction of that bar's close relative to
its open. The mechanism is claimed to operate only after the first 30 minutes of
the session, when the opening auction and initial price discovery are complete
and a large range bar is more likely to reflect genuine information flow than
liquidity provision.

The strategy does not condition on month, weekday, or hour beyond the session
time filter. Any concentration in the results is a property of the data, not of
the strategy's design.
"""

from __future__ import annotations


class Strategy:
    """Trade continuation of outsized intra-session range bars after the open."""

    def __init__(self, *, interval: str = "5m", range_mult: float = 2.0,
                 stop_atr: float = 1.0, target_atr: float = 2.0,
                 min_elapsed_ns: int = 30 * 60 * 1_000_000_000,
                 size: int = 1):
        self.interval = interval
        self.range_mult = range_mult
        self.stop_atr = stop_atr
        self.target_atr = target_atr
        self.min_elapsed_ns = min_elapsed_ns
        self.size = size

        # Per-session state.
        self._session = None
        self._atr_sum = 0.0
        self._bar_count = 0
        self._prev_close = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Reset state when the session changes.
        if ctx.session != self._session:
            self._session = ctx.session
            self._atr_sum = 0.0
            self._bar_count = 0
            self._prev_close = None

        history = ctx.history(self.interval)
        if len(history) < 2:
            return

        bar = history[-1]

        # Update ATR with the bar that just closed.
        if self._prev_close is None:
            self._prev_close = bar.open
        tr = max(bar.high - bar.low,
                 abs(bar.high - self._prev_close),
                 abs(bar.low - self._prev_close))
        self._atr_sum += tr
        self._bar_count += 1
        self._prev_close = bar.close

        if self._bar_count < 2:
            return

        atr = self._atr_sum / self._bar_count
        if atr <= 0:
            return

        # Only trade after the first 30 minutes of the session.
        elapsed = ctx.ts - ctx.session_start
        if elapsed < self.min_elapsed_ns:
            return

        # One position at a time; the bracket handles exits.
        if ctx.position or ctx.working_order_ids:
            return

        # Outsized range bar: range exceeds multiple of session ATR.
        bar_range = bar.high - bar.low
        if bar_range < self.range_mult * atr:
            return

        # Continuation: trade in the direction of the bar's close vs open.
        if bar.close > bar.open:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_atr * atr,
                target_offset=self.target_atr * atr,
                tag="continuation-long",
            )
        else:
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
