"""Continuation of outsized intra-session moves, with tuned parameters.

Hypothesis: after a move that is large relative to the session's own realised
range so far, price continues in the same direction. We measure "outsized" as
the absolute change over the last `lookback` bars exceeding a fraction
(`threshold_mult`) of the session's high-low range so far. When triggered, we
enter in the direction of that move with a protective bracket sized off the
move itself.

Compared to the best prior trial (#0), we use a slightly longer lookback (4
bars instead of 3) and a lower threshold (0.45 instead of 0.5) to keep the
trigger rate high while capturing somewhat larger moves. The bracket is
adjusted to a tighter stop (0.8x move) and a wider target (2.0x move) to
improve the risk/reward ratio. These changes aim to increase expectancy while
maintaining exposure in at least 70% of sessions.
"""

from collections import deque


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        lookback: int = 4,
        threshold_mult: float = 0.45,
        stop_mult: float = 0.8,
        target_mult: float = 2.0,
        min_bars: int = 5,
        min_range: float = 0.5,
        size: int = 1,
    ):
        self.interval = interval
        self.lookback = lookback
        self.threshold_mult = threshold_mult
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.min_bars = min_bars
        self.min_range = min_range
        self.size = size

        # Session state
        self.session = None
        self.session_high = None
        self.session_low = None
        self.bars_in_session = 0
        self.recent_closes: deque[float] = deque(maxlen=lookback + 1)

    def setup(self, ctx):
        ctx.subscribe(self.interval)

    def on_bars(self, ctx):
        if not ctx.actionable:
            return

        # Reset on new session
        if ctx.session != self.session:
            self.session = ctx.session
            self.session_high = None
            self.session_low = None
            self.bars_in_session = 0
            self.recent_closes.clear()

        # Get the bar that just closed
        history = ctx.history(self.interval)
        if len(history) == 0:
            return
        bar = history[-1]

        # Update session state
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

        # Already in a position or have working orders
        if ctx.position or ctx.working_order_ids:
            return

        # Determine direction and place order with bracket
        stop_offset = min(self.stop_mult * abs(move), session_range * 0.5)
        target_offset = min(self.target_mult * abs(move), session_range * 1.0)
        if move > 0:
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="outsized-continuation-long",
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="outsized-continuation-short",
            )

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
