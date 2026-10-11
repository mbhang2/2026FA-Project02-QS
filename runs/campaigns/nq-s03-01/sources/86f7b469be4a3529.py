"""Investigate whether an outsized move inside a session continues or gives back.

This strategy tests a continuation hypothesis with a focus on the *second* leg
of an outsized move, rather than the first. Prior attempts entered immediately
after the move was identified and were often stopped out by the inevitable
pullback. Here we wait for the pullback itself and enter on the resumption of
the move, with a tighter stop placed just beyond the pullback extreme.

The trigger is a move from the session open that is large relative to the
session's range so far. After the trigger bar, we wait for a pullback bar that
closes against the move direction. We then place a stop order beyond the
pullback bar's extreme in the move direction, so we only enter if price resumes
the move. The stop is tighter than in prior attempts because it is placed just
beyond the pullback, reducing the distance to the entry and therefore the risk
per trade.

The entry is a stop order with a bracket. The bracket's stop and target are
given as offsets from the fill price. The target is wider than the stop to give
the continuation room to run, while the stop is tight to limit losses if the
resumption fails.
"""

from __future__ import annotations


class Strategy:
    """Trade continuation after a pullback following an outsized move.

    An outsized move is one whose distance from the session open, relative to
    the session's range so far, exceeds a threshold. After such a move, we wait
    for a pullback bar that closes against the move direction. We then place a
    stop entry order beyond the pullback bar's extreme in the move direction.
    If price resumes the move, we enter with a tight stop just beyond the
    pullback extreme.
    """

    def __init__(
        self,
        *,
        interval: str = "5m",
        threshold: float = 0.65,
        size: int = 1,
        stop_offset: float = 0.5,
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

        # State for the pullback entry pattern.
        self._trigger_direction: int = 0  # 1 for long, -1 for short, 0 for none
        self._trigger_bar_close: float | None = None
        self._pullback_extreme: float | None = None
        self._entry_order_tag: str | None = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
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
            self._trigger_bar_close = None
            self._pullback_extreme = None
            self._entry_order_tag = None
        else:
            # These are not None after the first bar of a session.
            self._session_high = max(self._session_high, bar.high)
            self._session_low = min(self._session_low, bar.low)

        # If we have a working entry order, do nothing.
        if self._entry_order_tag is not None:
            orders = ctx.find_orders(tag=self._entry_order_tag)
            if orders:
                return
            # Order was filled or expired; clear state.
            self._trigger_direction = 0
            self._trigger_bar_close = None
            self._pullback_extreme = None
            self._entry_order_tag = None
            return

        # If we have a position, do nothing.
        if ctx.position:
            return

        # Check if we are waiting for a pullback after a trigger.
        if self._trigger_direction != 0 and self._trigger_bar_close is not None:
            direction = self._trigger_direction
            trigger_close = self._trigger_bar_close

            # A pullback bar closes against the move direction.
            if direction > 0 and bar.close < trigger_close:
                # Record the pullback extreme (low for a long entry).
                self._pullback_extreme = bar.low
                # Place a stop entry order above the pullback extreme.
                entry_price = self._pullback_extreme + 0.25
                self._entry_order_tag = "pullback-continuation-long"
                ctx.buy(
                    size=self.size,
                    type="stop",
                    trigger=entry_price,
                    stop_offset=self.stop_offset,
                    target_offset=self.target_offset,
                    tag=self._entry_order_tag,
                )
                # Clear trigger state; entry order is now working.
                self._trigger_direction = 0
                self._trigger_bar_close = None
                return
            elif direction < 0 and bar.close > trigger_close:
                # Record the pullback extreme (high for a short entry).
                self._pullback_extreme = bar.high
                # Place a stop entry order below the pullback extreme.
                entry_price = self._pullback_extreme - 0.25
                self._entry_order_tag = "pullback-continuation-short"
                ctx.sell(
                    size=self.size,
                    type="stop",
                    trigger=entry_price,
                    stop_offset=self.stop_offset,
                    target_offset=self.target_offset,
                    tag=self._entry_order_tag,
                )
                # Clear trigger state; entry order is now working.
                self._trigger_direction = 0
                self._trigger_bar_close = None
                return

            # If the bar continues in the move direction without a pullback,
            # we do not chase it. Clear the trigger state.
            self._trigger_direction = 0
            self._trigger_bar_close = None
            return

        # No pending trigger: check for a new outsized move.
        session_range = self._session_high - self._session_low
        if session_range <= 0:
            return

        move = bar.close - self._session_open
        relative_move = abs(move) / session_range

        if relative_move < self.threshold:
            return

        # Store the trigger for the next bar.
        self._trigger_direction = 1 if move > 0 else -1
        self._trigger_bar_close = bar.close

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
