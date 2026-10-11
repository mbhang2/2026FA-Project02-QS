"""Investigate whether an outsized move inside a session continues or gives back.

This strategy tests the give-back hypothesis with a different entry mechanism
than prior attempts. Instead of entering on a limit order after a retracement
bar, we enter on the *next bar* after the trigger, using a market order but
only if the trigger bar itself was a strong directional bar that closed near
its extreme. The idea is that an outsized move that closes strong is more
likely to be a genuine impulse that will exhaust and give back, rather than a
trend that continues. We enter against the move immediately, with a tight stop
beyond the trigger bar's extreme and a target near the session open.

Prior give-back attempts (#9, #11, #14) entered either immediately or on a
limit order after a retracement. #14's limit order often never filled, leaving
the strategy unexposed. This strategy enters on the bar after the trigger,
which guarantees exposure to the give-back if it occurs, while the tight stop
limits losses if the move continues.

The trigger is a move from the session open that is large relative to the
session's range so far, with the trigger bar closing in the top or bottom
quartile of its range. We then enter a market order in the opposite direction
on the next bar, with a stop beyond the trigger bar's extreme and a target at
the session open.
"""

from __future__ import annotations


class Strategy:
    """Trade give-back after an outsized move with a strong close.

    An outsized move is one whose distance from the session open, relative to
    the session's range so far, exceeds a threshold. The trigger bar must close
    in the top or bottom quartile of its range, confirming strong directional
    pressure. We then enter against the move on the next bar with a market
    order, a tight stop beyond the trigger extreme, and a target at the session
    open.
    """

    def __init__(
        self,
        *,
        interval: str = "5m",
        threshold: float = 0.7,
        close_fraction: float = 0.75,
        size: int = 1,
        stop_offset: float = 0.5,
        target_offset: float = 1.0,
    ):
        self.interval = interval
        self.threshold = threshold
        self.close_fraction = close_fraction
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset

        # Session-level state, kept across callbacks.
        self._session = None
        self._session_open: float | None = None
        self._session_high: float | None = None
        self._session_low: float | None = None

        # State for the trigger.
        self._trigger_direction: int = 0  # 1 for long move, -1 for short move
        self._trigger_extreme: float | None = None  # extreme price of the move

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return

        history = ctx.history(self.interval)
        if len(history) < 2:
            return

        bar = history[-1]
        prev_bar = history[-2]

        # Track session boundaries. Bars never span a session, so a change in
        # ctx.session means this is the first bar of a new session.
        if ctx.session != self._session:
            self._session = ctx.session
            self._session_open = bar.open
            self._session_high = bar.high
            self._session_low = bar.low
            self._trigger_direction = 0
            self._trigger_extreme = None
            return

        # These are not None after the first bar of a session.
        self._session_high = max(self._session_high, bar.high)
        self._session_low = min(self._session_low, bar.low)

        # If we have a pending trigger from the previous bar, act on it now.
        if self._trigger_direction != 0 and self._trigger_extreme is not None:
            direction = self._trigger_direction
            extreme = self._trigger_extreme
            session_open = self._session_open

            if session_open is None:
                return

            # Enter against the move.
            if direction > 0:
                # Long move: enter short.
                ctx.sell(
                    size=self.size,
                    stop_offset=self.stop_offset,
                    target_offset=self.target_offset,
                    tag="give-back-short",
                )
            else:
                # Short move: enter long.
                ctx.buy(
                    size=self.size,
                    stop_offset=self.stop_offset,
                    target_offset=self.target_offset,
                    tag="give-back-long",
                )

            # Clear the trigger.
            self._trigger_direction = 0
            self._trigger_extreme = None
            return

        # No pending trigger: check for a new outsized move with a strong close.
        session_range = self._session_high - self._session_low
        if session_range <= 0:
            return

        if self._session_open is None:
            return

        move = bar.close - self._session_open
        relative_move = abs(move) / session_range

        if relative_move < self.threshold:
            return

        # Check that the trigger bar closed near its extreme in the move direction.
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return

        if move > 0:
            close_position = (bar.close - bar.low) / bar_range
        else:
            close_position = (bar.high - bar.close) / bar_range

        if close_position < self.close_fraction:
            return

        # Store the trigger for the next bar.
        self._trigger_direction = 1 if move > 0 else -1
        self._trigger_extreme = bar.high if move > 0 else bar.low

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
