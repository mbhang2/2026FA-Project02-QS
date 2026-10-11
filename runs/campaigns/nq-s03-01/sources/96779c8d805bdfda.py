"""Investigate whether an outsized move inside a session continues or gives back.

This strategy tests a refined continuation hypothesis with a multi-bar confirmation
and a tighter exit structure. The prior continuation attempt (#0) traded every
outsized move and lost money; #3 added a single-bar confirmation and improved
but stayed negative. Here the confirmation requires the outsized move to be
sustained over two consecutive bars, both closing strongly in the move direction.
This filters out single-bar spikes and focuses on moves with persistent
directional pressure.

The entry is a market order with a bracket, as in #0 and #3. The bracket
protects the position from the moment of entry. The stop is wider than the
target to give the continuation room to breathe while still capping losses.
"""

from __future__ import annotations


class Strategy:
    """Trade continuation of a sustained, outsized intra-session move.

    An outsized move is one whose distance from the session open, relative to
    the session's range so far, exceeds a threshold. The move must be confirmed
    by two consecutive strong bars: both bars must have a body that is at least
    a minimum fraction of their range, and both must close in the direction
    of the move.
    """

    def __init__(
        self,
        *,
        interval: str = "5m",
        threshold: float = 0.75,
        body_fraction: float = 0.55,
        close_fraction: float = 0.65,
        size: int = 1,
        stop_offset: float = 1.5,
        target_offset: float = 1.0,
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

        # Both the current and previous bar must be strong bars in the
        # direction of the move.
        if not self._is_strong_bar(bar, move):
            return
        if not self._is_strong_bar(prev_bar, move):
            return

        if move > 0:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="sustained-continuation-long",
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="sustained-continuation-short",
            )

    def _is_strong_bar(self, bar, move: float) -> bool:
        """Return True if the bar is strong in the direction of the move."""
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return False

        body = abs(bar.close - bar.open)
        if body / bar_range < self.body_fraction:
            return False

        if move > 0:
            return (bar.close - bar.low) / bar_range >= self.close_fraction
        else:
            return (bar.high - bar.close) / bar_range >= self.close_fraction

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
