"""Investigate whether an outsized move inside a session continues or gives back.

Measures "large" relative to the session's own realised range so far: the
distance from the session's open to the latest close, divided by the session's
high-low range up to that point. A move that has already covered most of the
session's range is outsized. The trade takes the continuation side: if the move
is up and outsized, buy; if down and outsized, sell. A bracket gives back the
protection a hand-rolled stop would miss.

The feature is deliberately simple and uses only closed bars. No tick
subscription, so the walk stays event-driven and fast.
"""

from __future__ import annotations


class Strategy:
    """Trade continuation of an outsized intra-session move.

    An outsized move is one whose distance from the session open, relative to
    the session's range so far, exceeds a threshold. The threshold is a
    parameter, not fitted, so this is an ordinary strategy.
    """

    def __init__(
        self,
        *,
        interval: str = "5m",
        threshold: float = 0.8,
        size: int = 1,
        stop_offset: float = 1.0,
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
        # At this point both values are set, so the subtraction is safe.
        session_range = self._session_high - self._session_low
        if session_range <= 0:
            return

        move = bar.close - self._session_open
        relative_move = abs(move) / session_range

        if relative_move < self.threshold:
            return

        # Continuation: take the side of the outsized move.
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
