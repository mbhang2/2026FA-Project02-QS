"""Investigate whether an outsized intra-session move continues, with order-flow confirmation.

Hypothesis: after a move that is large relative to the session's own realised
range so far, price continues in the same direction, especially when the move
aligns with the session's overall direction and is supported by order flow.
We measure "outsized" as the absolute change over the last `lookback` bars
exceeding a fraction (`threshold_mult`) of the session's high-low range so far.
We require the triggering bar's signed delta (buy_volume - sell_volume) to be
in the same direction as the move and to represent a meaningful fraction of
the bar's volume, confirming that the move is driven by aggressive order flow
rather than a few large prints. The session range is tracked incrementally
from closed bars, so no lookahead. We require a minimum number of bars and a
minimum range before acting to avoid noise. The bracket offsets are multiples
of the triggering move, capped to avoid absurd levels.
"""

from collections import deque


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        lookback: int = 3,
        threshold_mult: float = 0.5,
        stop_mult: float = 0.6,
        target_mult: float = 2.5,
        min_bars: int = 5,
        min_range: float = 0.5,
        direction_threshold: float = 0.25,
        delta_ratio_min: float = 0.3,
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
        self.delta_ratio_min = delta_ratio_min
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

        # Order-flow confirmation: the triggering bar's delta must be in the
        # same direction as the move and represent a meaningful fraction of
        # the bar's volume.
        if bar.volume <= 0:
            return
        delta_ratio = abs(bar.delta) / bar.volume
        if delta_ratio < self.delta_ratio_min:
            return
        if (move > 0 and bar.delta <= 0) or (move < 0 and bar.delta >= 0):
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
        target_offset = min(self.target_mult * abs(move), session_range * 1.0)
        if move > 0:
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="outsized-delta-confirm-long",
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="outsized-delta-confirm-short",
            )

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
