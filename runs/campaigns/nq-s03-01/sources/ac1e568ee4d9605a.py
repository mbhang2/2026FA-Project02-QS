"""Investigate whether an outsized move inside a session continues or gives back.

This strategy tests a give-back hypothesis with a mean-reversion entry after an
outsized move, using a limit order to fade the move. Prior continuation attempts
(#0, #3, #5, #8, #10, #12, #13) all lost money, and the give-back attempts
(#9, #11) also lost money. However, those give-back attempts entered immediately
on the trigger bar with a market order, paying the spread and often getting run
over if the move continued. This strategy waits for a pullback signal and then
places a limit order to enter in the opposite direction of the outsized move,
only if price retraces to a favorable level. This gives a better entry price and
avoids chasing the move.

The trigger is a move from the session open that is large relative to the
session's range so far. After the trigger, we wait for a bar that closes back
toward the session open (a retracement bar). We then place a limit order at a
level that is a fraction of the way back toward the open, in the direction of
the give-back. The limit order only fills if price actually retraces, so we do
not get run over if the move continues. The bracket is tight, with a stop just
beyond the trigger extreme and a target at the session open.
"""

from __future__ import annotations


class Strategy:
    """Trade give-back after an outsized move using a limit order.

    An outsized move is one whose distance from the session open, relative to
    the session's range so far, exceeds a threshold. After such a move, we wait
    for a retracement bar that closes back toward the session open. We then
    place a limit order at a level that is a fraction of the way back toward
    the open, in the direction of the give-back. The limit order only fills if
    price actually retraces, so we do not get run over if the move continues.
    """

    def __init__(
        self,
        *,
        interval: str = "5m",
        threshold: float = 0.7,
        retracement_fraction: float = 0.5,
        size: int = 1,
        stop_offset: float = 0.5,
        target_offset: float = 1.0,
    ):
        self.interval = interval
        self.threshold = threshold
        self.retracement_fraction = retracement_fraction
        self.size = size
        self.stop_offset = stop_offset
        self.target_offset = target_offset

        # Session-level state, kept across callbacks.
        self._session = None
        self._session_open: float | None = None
        self._session_high: float | None = None
        self._session_low: float | None = None

        # State for the give-back entry pattern.
        self._trigger_direction: int = 0  # 1 for long move, -1 for short move
        self._trigger_extreme: float | None = None  # extreme price of the move
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

        # Track session boundaries. Bars never span a session, so a change in
        # ctx.session means this is the first bar of a new session.
        if ctx.session != self._session:
            self._session = ctx.session
            self._session_open = bar.open
            self._session_high = bar.high
            self._session_low = bar.low
            self._trigger_direction = 0
            self._trigger_extreme = None
            self._entry_order_tag = None
        else:
            # These are not None after the first bar of a session.
            self._session_high = max(self._session_high, bar.high)
            self._session_low = min(self._session_low, bar.low)

        # If we have a working entry order, check if it is still active.
        if self._entry_order_tag is not None:
            orders = ctx.find_orders(tag=self._entry_order_tag)
            if orders:
                return
            # Order was filled, expired, or cancelled; clear state.
            self._trigger_direction = 0
            self._trigger_extreme = None
            self._entry_order_tag = None
            return

        # If we have a position, do nothing.
        if ctx.position:
            return

        # Check if we are waiting for a retracement after a trigger.
        if self._trigger_direction != 0 and self._trigger_extreme is not None:
            direction = self._trigger_direction
            extreme = self._trigger_extreme
            session_open = self._session_open

            # A retracement bar closes back toward the session open.
            if direction > 0 and bar.close < extreme:
                # Long move: fade it with a short limit order.
                # Place the limit at a level that is a fraction of the way back
                # toward the open.
                if session_open is None:
                    return
                limit_price = extreme - self.retracement_fraction * (extreme - session_open)
                self._entry_order_tag = "give-back-short"
                ctx.sell(
                    size=self.size,
                    type="limit",
                    limit=limit_price,
                    stop_offset=self.stop_offset,
                    target_offset=self.target_offset,
                    tag=self._entry_order_tag,
                )
                self._trigger_direction = 0
                self._trigger_extreme = None
                return
            elif direction < 0 and bar.close > extreme:
                # Short move: fade it with a long limit order.
                if session_open is None:
                    return
                limit_price = extreme + self.retracement_fraction * (session_open - extreme)
                self._entry_order_tag = "give-back-long"
                ctx.buy(
                    size=self.size,
                    type="limit",
                    limit=limit_price,
                    stop_offset=self.stop_offset,
                    target_offset=self.target_offset,
                    tag=self._entry_order_tag,
                )
                self._trigger_direction = 0
                self._trigger_extreme = None
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
