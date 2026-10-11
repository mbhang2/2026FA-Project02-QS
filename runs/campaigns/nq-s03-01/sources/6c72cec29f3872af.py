"""Investigate whether an outsized move inside a session continues or gives back.

This strategy refines the give-back hypothesis from trial #1. The prior fade
attempt used a fixed limit offset and a lower threshold, but still lost money.
The refinement here makes the fade conditional on the move being not just large
relative to the session range, but also on the move having occurred quickly --
an outsized move that is also fast is more likely to be a temporary imbalance
that gives back.

The feature combines two session-level measures:
1. Relative move: absolute distance from session open divided by session range
   so far.
2. Move speed: the relative move divided by the number of bars elapsed in the
   session.

A move that is both outsized and fast triggers a fade. The entry uses a limit
order to avoid paying the spread on entry, and the bracket protects the
position. The threshold is higher than trial #1 because the speed filter
reduces false signals, allowing a more selective entry.
"""

from __future__ import annotations


class Strategy:
    """Trade the give-back of a fast, outsized intra-session move.

    An outsized move is one whose distance from the session open, relative to
    the session's range so far, exceeds a threshold. A fast move is one whose
    relative move per bar exceeds a speed threshold. When both conditions hold,
    fade the move.
    """

    def __init__(
        self,
        *,
        interval: str = "5m",
        threshold: float = 0.75,
        speed_threshold: float = 0.06,
        size: int = 1,
        limit_offset: float = 0.5,
        stop_offset: float = 1.0,
        target_offset: float = 1.5,
    ):
        self.interval = interval
        self.threshold = threshold
        self.speed_threshold = speed_threshold
        self.size = size
        self.limit_offset = limit_offset
        self.stop_offset = stop_offset
        self.target_offset = target_offset

        # Session-level state, kept across callbacks.
        self._session = None
        self._session_open: float | None = None
        self._session_high: float | None = None
        self._session_low: float | None = None
        self._bars_this_session = 0

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
            self._bars_this_session = 1
        else:
            self._bars_this_session += 1
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

        # Speed filter: outsized move must have happened quickly, not gradually.
        speed = relative_move / self._bars_this_session
        if speed < self.speed_threshold:
            return

        # Give-back: fade the fast, outsized move with a limit order.
        if move > 0:
            # Price moved up quickly; sell into strength above the market.
            ctx.sell(
                size=self.size,
                type="limit",
                limit=ctx.last_price + self.limit_offset,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="giveback-fast-short",
            )
        else:
            # Price moved down quickly; buy into weakness below the market.
            ctx.buy(
                size=self.size,
                type="limit",
                limit=ctx.last_price - self.limit_offset,
                stop_offset=self.stop_offset,
                target_offset=self.target_offset,
                tag="giveback-fast-long",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
