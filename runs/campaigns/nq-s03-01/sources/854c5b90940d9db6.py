"""Investigate whether an outsized move inside a session continues or gives back.

This strategy tests a refined give-back hypothesis using a limit entry and a
tighter, more selective trigger. Prior give-back attempt (#4) lost money, but
it used a limit entry that may have been too passive. Here the fade is
conditioned on the move being extreme relative to the session range AND on the
triggering bar showing rejection: the bar closes back inside its range after
making a large excursion, indicating the move is exhausting.

The entry is a market order rather than a limit order, which guarantees a fill
and avoids the risk of missing the move entirely. The bracket protects the
position from the moment of entry. The stop is wider than the target to give
the give-back room to develop while still capping losses.
"""

from __future__ import annotations


class Strategy:
    """Trade the give-back of an extreme, rejected intra-session move.

    An outsized move is one whose distance from the session open, relative to
    the session's range so far, exceeds a high threshold. The move must also
    show rejection on the triggering bar: the bar's close must be a long way
    from its extreme in the direction of the move, indicating that the move
    ran out of steam.
    """

    def __init__(
        self,
        *,
        interval: str = "5m",
        threshold: float = 0.9,
        rejection_fraction: float = 0.5,
        size: int = 1,
        stop_offset: float = 1.0,
        target_offset: float = 1.5,
    ):
        self.interval = interval
        self.threshold = threshold
        self.rejection_fraction = rejection_fraction
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

        # Rejection: the bar's close must be far from its extreme in the
        # direction of the move. For an up move, the close should be well
        # below the high; for a down move, well above the low.
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            return

        if move > 0:
            # Up move: rejection means close is far below the high.
            if (bar.high - bar.close) / bar_range < self.rejection_fraction:
                return
            # Fade the move by selling into strength.
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="giveback-rejection-short",
            )
        else:
            # Down move: rejection means close is far above the low.
            if (bar.close - bar.low) / bar_range < self.rejection_fraction:
                return
            # Fade the move by buying into weakness.
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="giveback-rejection-long",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
