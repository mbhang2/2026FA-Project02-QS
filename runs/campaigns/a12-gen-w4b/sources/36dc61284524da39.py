"""Investigate whether an outsized intra-session move continues or gives back.

Hypothesis: after a move that is large relative to the session's own realised
range so far, price continues in the same direction. We measure "large" as the
current move from the session's opening range being at least k times the
session's average true range so far, and enter in the direction of the move
with a bracket. The bracket captures the continuation while limiting give-back.
"""

from __future__ import annotations


class Strategy:
    """Trade continuation after an outsized intra-session move."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 6,
        move_mult: float = 2.0,
        size: int = 1,
        stop_offset: float = 1.0,
        target_offset: float = 1.5,
    ):
        self.interval = interval
        self.lookback = lookback
        self.move_mult = move_mult
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset
        self._session = None
        self._session_bars = 0
        self._session_highs: list[float] = []
        self._session_lows: list[float] = []
        self._session_ranges: list[float] = []

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if ctx.session != self._session:
            self._session = ctx.session
            self._session_bars = 0
            self._session_highs = []
            self._session_lows = []
            self._session_ranges = []

        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
        if len(history) < self.lookback + 1:
            return

        # Update session state with the bar that just closed.
        bar = history[-1]
        self._session_bars += 1
        self._session_highs.append(bar.high)
        self._session_lows.append(bar.low)
        if len(self._session_highs) > 1:
            prev_high = self._session_highs[-2]
            prev_low = self._session_lows[-2]
            self._session_ranges.append(
                max(bar.high, prev_high) - min(bar.low, prev_low)
            )

        # Need enough bars to have a meaningful average range.
        if len(self._session_ranges) < self.lookback:
            return

        avg_range = sum(self._session_ranges[-self.lookback:]) / self.lookback
        if avg_range <= 0:
            return

        # The move from the session's opening range to the current close.
        session_open = history[0].open
        current_close = bar.close
        move = current_close - session_open
        if abs(move) < self.move_mult * avg_range:
            return

        # Continuation: enter in the direction of the move.
        if move > 0:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="continuation-long",
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="continuation-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
