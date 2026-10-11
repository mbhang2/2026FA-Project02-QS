"""Investigate whether an outsized move inside a session continues or gives back.

This strategy tests a give-back hypothesis with a mean-reversion trigger based
on the session's volume-weighted average price (VWAP). The idea is that a move
far from the session VWAP, relative to the session's range so far, is an
overreaction and price tends to revert toward the VWAP.

Prior continuation attempts (#0, #3, #5, #8, #10) all lost money. The simple
give-back attempt (#9) also lost money but traded far too often. This strategy
adds a confirmation filter: the move must be large relative to the session's
range AND the current bar must close beyond the prior bar's extreme in the
direction of the move, suggesting the move is still active. We then enter in
the opposite direction, expecting reversion.

The entry is a market order with a bracket. The stop is wider than the target
to give the reversion room to develop while still capping losses.
"""

from __future__ import annotations


class Strategy:
    """Trade give-back of an outsized move from the session VWAP.

    An outsized move is one whose distance from the session VWAP, relative to
    the session's range so far, exceeds a threshold. The move must be confirmed
    by the current bar closing beyond the prior bar's extreme in the move
    direction. The strategy enters in the opposite direction, expecting price
    to revert toward the VWAP.
    """

    def __init__(
        self,
        *,
        interval: str = "5m",
        threshold: float = 0.75,
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
        self._session_volume: int = 0
        self._session_vwap_num: float = 0.0

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
            self._session_volume = bar.volume
            self._session_vwap_num = (bar.high + bar.low + bar.close) / 3.0 * bar.volume
        else:
            # These are not None after the first bar of a session.
            self._session_high = max(self._session_high, bar.high)
            self._session_low = min(self._session_low, bar.low)
            self._session_volume += bar.volume
            self._session_vwap_num += (bar.high + bar.low + bar.close) / 3.0 * bar.volume

        # Need at least a little range and volume before the measure means anything.
        session_range = self._session_high - self._session_low
        if session_range <= 0 or self._session_volume <= 0:
            return

        vwap = self._session_vwap_num / self._session_volume
        move = bar.close - vwap
        relative_move = abs(move) / session_range

        if relative_move < self.threshold:
            return

        # Confirmation: the current bar must close beyond the prior bar's
        # extreme in the direction of the move.
        if move > 0 and bar.close <= prev_bar.high:
            return
        if move < 0 and bar.close >= prev_bar.low:
            return

        # Enter in the opposite direction of the move.
        if move > 0:
            ctx.sell(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="vwap-give-back-short",
            )
        else:
            ctx.buy(
                size=self.size,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="vwap-give-back-long",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
