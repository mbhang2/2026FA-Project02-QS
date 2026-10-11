"""Investigate whether an outsized intra-session move tends to continue or give back.

This strategy tests the give-back hypothesis, but with a crucial refinement: the
outsized move is defined relative to the session's own realized range so far,
and the entry is delayed until the bar that closed after the outsized move shows
a reversal. This avoids the problem of entering immediately against a move that
may still be in progress, and it filters for the moment when the imbalance
actually begins to unwind.

The mechanism is claimed to operate throughout the session, not just in specific
hours, because the temporary liquidity imbalance that creates an outsized move
can occur at any time. The strategy does not condition on month, weekday, or
hour. Any concentration in the results is a property of the data, not of the
strategy's design.

The key difference from prior attempts is the confirmation bar: instead of
entering on the outsized bar itself, we wait for the next bar to close in the
opposite direction of the move, confirming that the give-back has begun. This
reduces false entries and respects the T+1 rule naturally.
"""

from __future__ import annotations


class Strategy:
    """Trade give-back of outsized moves with a confirmation bar."""

    def __init__(self, *, interval: str = "5m", range_mult: float = 2.0,
                 stop_atr: float = 1.0, target_atr: float = 2.0,
                 size: int = 1):
        self.interval = interval
        self.range_mult = range_mult
        self.stop_atr = stop_atr
        self.target_atr = target_atr
        self.size = size

        # Per-session state.
        self._session = None
        self._atr_sum = 0.0
        self._bar_count = 0
        self._prev_close = None
        self._prev_bar_outsized = False
        self._prev_bar_direction = 0  # 1 for up, -1 for down

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
            self._prev_bar_outsized = False
            self._prev_bar_direction = 0

        history = ctx.history(self.interval)
        if len(history) < 3:
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

        if self._bar_count < 3:
            return

        atr = self._atr_sum / self._bar_count
        if atr <= 0:
            return

        # One position at a time; the bracket handles exits.
        if ctx.position or ctx.working_order_ids:
            return

        # Check if the previous bar was outsized.
        prev_bar = history[-2]
        prev_range = prev_bar.high - prev_bar.low
        prev_outsized = prev_range >= self.range_mult * atr

        if prev_outsized:
            # Determine direction of the outsized move.
            prev_direction = 1 if prev_bar.close > prev_bar.open else -1
            # Current bar must show reversal: close in opposite direction.
            current_direction = 1 if bar.close > bar.open else -1
            if current_direction == -prev_direction:
                # Confirmed give-back: trade against the outsized move.
                if prev_direction > 0:  # outsized up move, now reversing down
                    ctx.sell(
                        size=self.size,
                        stop_offset=self.stop_atr * atr,
                        target_offset=self.target_atr * atr,
                        tag="giveback-short",
                    )
                else:  # outsized down move, now reversing up
                    ctx.buy(
                        size=self.size,
                        stop_offset=self.stop_atr * atr,
                        target_offset=self.target_atr * atr,
                        tag="giveback-long",
                    )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
