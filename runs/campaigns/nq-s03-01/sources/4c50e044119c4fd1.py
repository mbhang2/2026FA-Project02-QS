"""Investigate whether an outsized move inside a session continues or gives back.

This strategy tests the give-back hypothesis: after a large move away from the
session open, price tends to revert toward the open. The prior attempts all
traded continuation and lost money. Here we enter in the opposite direction of
the move, betting that the move was an overreaction.

The trigger is a move from the session open whose magnitude, relative to the
session's range so far, exceeds a threshold. The entry is a market order with a
bracket. The stop is tighter than the target, reflecting the expectation of a
reversion rather than a continuation.
"""

from __future__ import annotations


class Strategy:
    """Trade give-back of an outsized intra-session move.

    An outsized move is one whose distance from the session open, relative to
    the session's range so far, exceeds a threshold. The strategy enters in the
    opposite direction of the move, expecting price to revert toward the open.
    """

    def __init__(
        self,
        *,
        interval: str = "5m",
        threshold: float = 0.75,
        size: int = 1,
        stop_offset: float = 0.75,
        target_offset: float = 1.5,
    ):
        self.interval = interval
        self.threshold = threshold
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset

        # Session-level state, kept across callbacks.
        self._session = None
        self._session_open: float | None = None
        self._session_high: float | None = None
        self._session_low: float | None = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        bar = history[-1]

        # Track session boundaries. Bars never span a session, so a change in
        # ctx.session means this is the first bar of a new session.
        if ctx.session != self._session:
            self._session = ctx.session
            self._session_open = bar.open
            self._session_high = bar.high
            self._session_low = bar.low
        else:
            # These are not None after the first bar of a session.
            self._session_high = max(self._session_high, bar.high)
            self._session_low = min(self._session_low, bar.low)

        # Need at least a little range before the measure means anything.
        session_range = self._session_high - self._session_low
        if session_range <= 0:
            return

        move = bar.close - self._session_open
        relative_move = abs(move) / session_range

        if relative_move < self.threshold:
            return

        # Enter in the opposite direction of the move.
        if move > 0:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="give-back-short",
            )
        else:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="give-back-long",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
