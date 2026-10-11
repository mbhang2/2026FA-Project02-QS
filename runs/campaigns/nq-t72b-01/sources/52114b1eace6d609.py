"""Refine the continuation hypothesis with a longer lookback and stricter trend alignment.

Hypothesis: after a move that is large relative to the session's own realised
range so far, price continues in the same direction, especially when the move
aligns with the session's overall direction. We measure "outsized" as the
absolute change over the last `lookback` bars exceeding a fraction
(`threshold_mult`) of the session's high-low range so far. When triggered, we
enter in the direction of that move with a protective bracket sized off the
move itself.

This variant uses a longer lookback (5 bars) to capture a more sustained move,
a higher threshold (0.6) to reduce noise, and a tighter stop (0.5) with a
wider target (3.0) to let winners run. The directional filter is slightly
more permissive (direction_threshold=0.2) to allow trades when the session
has a moderate trend. We require at least 10 bars in the session to avoid
early noise. The session range is tracked incrementally from closed bars, so
no lookahead.
"""

from collections import deque


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        lookback: int = 5,
        threshold_mult: float = 0.6,
        stop_mult: float = 0.5,
        target_mult: float = 3.0,
        min_bars: int = 10,
        min_range: float = 0.5,
        direction_threshold: float = 0.2,
        size: int = 1,
    ):
        self.interval = interval
        self.lookback = lookback
        self.threshold_mult = threshold_mult
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.min_bars = min_bars
        self.min_range = min_range
        self.direction_threshold = direction_threshold
        self.size = size

        # Session state
        self.session = None
        self.session_high = None
        self.session_low = None
        self.session_open = None
        self.bars_in_session = 0
        self.recent_closes: deque[float] = deque(maxlen=lookback + 1)

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    def on_bars(self, ctx):
        if not ctx.actionable:
            return

        # Update session state on session change
        if ctx.session != self.session:
            self.session = ctx.session
            self.session_high = None
            self.session_low = None
            self.session_open = None
            self.bars_in_session = 0
            self.recent_closes.clear()

        history = ctx.history(self.interval)
        if len(history) == 0:
            return
        bar = history[-1]  # the bar that just closed

        # First bar of session: set open
        if self.session_open is None:
            self.session_open = bar.open

        self.bars_in_session += 1
        if self.session_high is None or bar.high > self.session_high:
            self.session_high = bar.high
        if self.session_low is None or bar.low < self.session_low:
            self.session_low = bar.low
        self.recent_closes.append(bar.close)

        # Need enough bars and a meaningful range
        if self.bars_in_session < self.min_bars:
            return
        session_range = self.session_high - self.session_low
        if session_range < self.min_range:
            return

        # Need enough closes to compute the move
        if len(self.recent_closes) < self.lookback + 1:
            return

        # Compute move over lookback bars
        move = self.recent_closes[-1] - self.recent_closes[0]
        if abs(move) < self.threshold_mult * session_range:
            return

        # Directional filter: if session has a clear direction, require alignment
        session_change = bar.close - self.session_open
        if abs(session_change) > self.direction_threshold * session_range:
            if move > 0 and session_change <= 0:
                return
            if move < 0 and session_change >= 0:
                return

        # Already in a position or have working orders
        if ctx.position or ctx.working_order_ids:
            return

        # Determine direction and place order with bracket
        stop_offset = min(self.stop_mult * abs(move), session_range * 0.5)
        target_offset = min(self.target_mult * abs(move), session_range * 1.5)
        if move > 0:
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="outsized-long-lookback5",
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="outsized-short-lookback5",
            )

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
