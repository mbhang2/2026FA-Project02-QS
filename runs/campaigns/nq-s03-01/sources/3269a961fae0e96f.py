"""Investigate whether an outsized move inside a session continues or gives back.

This strategy tests a continuation hypothesis with a delayed entry after the
outsized move, rather than entering immediately on the bar that triggers the
signal. Prior continuation attempts (#0, #3, #5, #8, #10) all lost money, and
the give-back attempts (#9, #11) also lost money. A common failure mode is
entering right at the extreme of a move and being immediately stopped out as
price pulls back. This strategy waits for one bar after the outsized move and
enters only if that bar closes in the direction of the move, confirming that
the move is not immediately reversing.

The trigger is based on the session's range so far: a move from the session
open that is large relative to the session's high-low range. The confirmation
bar must close beyond the trigger bar's close in the direction of the move,
showing that price has continued past the trigger point rather than stalling.
The entry is a market order with a bracket. The stop is wider than the target
to give the continuation room to breathe while still capping losses.
"""

from __future__ import annotations


class Strategy:
    """Trade continuation of an outsized move with a one-bar confirmation delay.

    An outsized move is one whose distance from the session open, relative to
    the session's range so far, exceeds a threshold. Instead of entering on the
    trigger bar, the strategy waits for the next bar and enters only if that
    bar closes beyond the trigger bar's close in the move direction. This
    filters out moves that immediately reverse.
    """

    def __init__(
        self,
        *,
        interval: str = "5m",
        threshold: float = 0.7,
        size: int = 1,
        stop_offset: float = 1.5,
        target_offset: float = 1.0,
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

        # Pending signal from the previous bar.
        self._pending_direction: int = 0  # 1 for long, -1 for short, 0 for none
        self._pending_trigger_close: float | None = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            # If we can't act, clear any pending signal to avoid stale entries.
            self._pending_direction = 0
            self._pending_trigger_close = None
            return

        history = ctx.history(self.interval)
        if len(history) < 2:
            return

        bar = history[-1]

        # Track session boundaries. Bars never span a session, so a change in
        # ctx.session means this is the first bar of a new session.
        if ctx.session != self._session:
            self._session = ctx.session
            self._session_open = bar.open
            self._session_high = bar.high
            self._session_low = bar.low
            self._pending_direction = 0
            self._pending_trigger_close = None
        else:
            # These are not None after the first bar of a session.
            self._session_high = max(self._session_high, bar.high)
            self._session_low = min(self._session_low, bar.low)

        # First, check if we have a pending signal from the previous bar.
        if self._pending_direction != 0 and self._pending_trigger_close is not None:
            direction = self._pending_direction
            trigger_close = self._pending_trigger_close

            # Clear the pending signal regardless of whether we act on it.
            self._pending_direction = 0
            self._pending_trigger_close = None

            # Confirmation: the current bar must close beyond the trigger bar's
            # close in the direction of the move.
            if direction > 0 and bar.close > trigger_close:
                ctx.buy(
                    size=self.size,
                    stop_offset=self.stop_offset,
                    target_offset=self.target_offset,
                    tag="delayed-continuation-long",
                )
                return
            elif direction < 0 and bar.close < trigger_close:
                ctx.sell(
                    size=self.size,
                    stop_offset=self.stop_offset,
                    target_offset=self.target_offset,
                    tag="delayed-continuation-short",
                )
                return

        # Now check if this bar triggers a new signal.
        session_range = self._session_high - self._session_low
        if session_range <= 0:
            return

        move = bar.close - self._session_open
        relative_move = abs(move) / session_range

        if relative_move < self.threshold:
            return

        # Store the signal for the next bar.
        self._pending_direction = 1 if move > 0 else -1
        self._pending_trigger_close = bar.close

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
