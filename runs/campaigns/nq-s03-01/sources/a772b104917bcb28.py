"""Investigate whether an outsized move inside a session continues or gives back.

This strategy tests the give-back hypothesis against the continuation hypothesis
from the prior trial. Instead of trading in the direction of the outsized move,
it trades against it: when price has moved far from the session open relative to
the session's range, fade the move. The entry uses a limit order to avoid paying
the spread on entry, and the bracket protects the position.

The measure of "outsized" is the same as the prior trial: absolute distance from
session open divided by session high-low range so far. The threshold is lower
here because fading requires acting earlier, before the move fully exhausts.
"""

from __future__ import annotations


class Strategy:
    """Trade the give-back of an outsized intra-session move.

    An outsized move is one whose distance from the session open, relative to
    the session's range so far, exceeds a threshold. This strategy takes the
    opposite side, expecting price to revert toward the session open.
    """

    def __init__(
        self,
        *,
        interval: str = "5m",
        threshold: float = 0.65,
        size: int = 1,
        limit_offset: float = 0.5,
        stop_offset: float = 1.0,
        target_offset: float = 1.5,
    ):
        self.interval = interval
        self.threshold = threshold
        self.size = size
        self.limit_offset = limit_offset
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

        # Give-back: fade the outsized move with a limit order.
        if move > 0:
            # Price moved up; sell into strength above the market.
            ctx.sell(
                size=self.size,
                type="limit",
                limit=ctx.last_price + self.limit_offset,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="giveback-short",
            )
        else:
            # Price moved down; buy into weakness below the market.
            ctx.buy(
                size=self.size,
                type="limit",
                limit=ctx.last_price - self.limit_offset,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="giveback-long",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
