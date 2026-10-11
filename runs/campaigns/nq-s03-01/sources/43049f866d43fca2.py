"""Investigate whether an outsized move inside a session continues or gives back.

This strategy tests a refined continuation hypothesis. The prior continuation
attempt (#0) traded every outsized move and lost money. The refinement here
requires the outsized move to be confirmed by a strong bar: the bar that pushes
the relative move over the threshold must itself have a large body and close
near its high (for up moves) or low (for down moves). This filters out moves
that are merely the result of a wide range with a small net change, and focuses
on bars where the move is being driven by sustained directional pressure.

The entry is a market order with a bracket, as in #0, because the continuation
claim is about momentum, not about getting a better fill. The bracket protects
the position from the moment of entry.
"""

from __future__ import annotations


class Strategy:
    """Trade continuation of a confirmed, outsized intra-session move.

    An outsized move is one whose distance from the session open, relative to
    the session's range so far, exceeds a threshold. The move must be confirmed
    by a strong bar: the bar that triggers the signal must have a body that is
    at least a minimum fraction of its range, and must close in the direction
    of the move.
    """

    def __init__(
        self,
        *,
        interval: str = "5m",
        threshold: float = 0.8,
        body_fraction: float = 0.6,
        close_fraction: float = 0.7,
        size: int = 1,
        stop_offset: float = 1.0,
        target_offset: float = 1.5,
    ):
        self.interval = interval
        self.threshold = threshold
        self.body_fraction = body_fraction
        self.close_fraction = close_fraction
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

        # Confirmation: the triggering bar must be a strong bar in the
        # direction of the move.
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return

        body = abs(bar.close - bar.open)
        if body / bar_range < self.body_fraction:
            return

        if move > 0:
            # For an up move, the bar should close near its high.
            if (bar.close - bar.low) / bar_range < self.close_fraction:
                return
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="confirmed-continuation-long",
            )
        else:
            # For a down move, the bar should close near its low.
            if (bar.high - bar.close) / bar_range < self.close_fraction:
                return
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="confirmed-continuation-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
