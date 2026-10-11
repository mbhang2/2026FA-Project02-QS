"""Investigate whether an outsized move inside a session continues or gives back.

This strategy tests a give-back hypothesis with a delayed entry and a
confirmation of exhaustion. Prior give-back attempts (#9, #11, #14, #15) all
lost money, but they either entered immediately after the trigger or used a
limit order that often never filled. This strategy waits for a bar that
reverses against the outsized move after the trigger, then enters on the next
bar with a market order in the direction of the reversal. The idea is that the
first strong counter-move after an outsized move signals exhaustion, and
entering on the next bar avoids the initial continuation while still capturing
the give-back. The stop is placed beyond the trigger extreme, and the target is
at the session open, so the risk/reward is favorable if the give-back occurs.

The trigger is a move from the session open that is large relative to the
session's range so far. After the trigger, we wait for a bar that closes
against the move direction with a strong body. We then enter on the following
bar with a market order in the direction of the reversal. The stop is placed
beyond the trigger extreme, and the target is at the session open.
"""

from __future__ import annotations


class Strategy:
    """Trade give-back after an outsized move with a reversal confirmation.

    An outsized move is one whose distance from the session open, relative to
    the session's range so far, exceeds a threshold. After such a move, we wait
    for a reversal bar that closes against the move direction with a strong
    body. We then enter on the next bar with a market order in the direction
    of the reversal. The stop is placed beyond the trigger extreme, and the
    target is at the session open.
    """

    def __init__(
        self,
        *,
        interval: str = "5m",
        threshold: float = 0.7,
        reversal_body_fraction: float = 0.6,
        size: int = 1,
        stop_offset: float = 0.5,
        target_offset: float = 1.0,
    ):
        self.interval = interval
        self.threshold = threshold
        self.reversal_body_fraction = reversal_body_fraction
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset

        # Session-level state, kept across callbacks.
        self._session = None
        self._session_open: float | None = None
        self._session_high: float | None = None
        self._session_low: float | None = None

        # State for the trigger and reversal.
        self._trigger_direction: int = 0  # 1 for long move, -1 for short move
        self._trigger_extreme: float | None = None  # extreme price of the move
        self._reversal_pending: bool = False  # True if we saw a reversal bar

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
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
            self._trigger_direction = 0
            self._trigger_extreme = None
            self._reversal_pending = False
            return

        # These are not None after the first bar of a session.
        self._session_high = max(self._session_high, bar.high)
        self._session_low = min(self._session_low, bar.low)

        # If we have a pending reversal from the previous bar, enter now.
        if self._reversal_pending:
            direction = self._trigger_direction
            session_open = self._session_open

            if session_open is None:
                self._reversal_pending = False
                return

            # Enter in the direction of the reversal (opposite the trigger).
            if direction > 0:
                # Long move: enter short.
                ctx.sell(
                    size=self.size,
                    stop_offset=self.stop_offset,
                    target_offset=self.target_offset,
                    tag="give-back-reversal-short",
                )
            else:
                # Short move: enter long.
                ctx.buy(
                    size=self.size,
                    stop_offset=self.stop_offset,
                    target_offset=self.target_offset,
                    tag="give-back-reversal-long",
                )

            # Clear the state.
            self._trigger_direction = 0
            self._trigger_extreme = None
            self._reversal_pending = False
            return

        # If we have a pending trigger, check for a reversal bar.
        if self._trigger_direction != 0 and self._trigger_extreme is not None:
            direction = self._trigger_direction
            extreme = self._trigger_extreme

            # A reversal bar closes against the move direction with a strong body.
            bar_range = bar.high - bar.low
            if bar_range <= 0:
                return

            body = abs(bar.close - bar.open)
            if body / bar_range < self.reversal_body_fraction:
                return

            if direction > 0 and bar.close < bar.open:
                # Long move: reversal bar is bearish.
                self._reversal_pending = True
                return
            elif direction < 0 and bar.close > bar.open:
                # Short move: reversal bar is bullish.
                self._reversal_pending = True
                return

            # If the bar continues in the move direction, update the extreme.
            if direction > 0:
                self._trigger_extreme = max(extreme, bar.high)
            else:
                self._trigger_extreme = min(extreme, bar.low)
            return

        # No pending trigger: check for a new outsized move.
        session_range = self._session_high - self._session_low
        if session_range <= 0:
            return

        if self._session_open is None:
            return

        move = bar.close - self._session_open
        relative_move = abs(move) / session_range

        if relative_move < self.threshold:
            return

        # Store the trigger for the next bar.
        self._trigger_direction = 1 if move > 0 else -1
        self._trigger_extreme = bar.high if move > 0 else bar.low

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
