"""Investigate whether an outsized intra-session move continues, with strict trend alignment and order-flow confirmation.

Hypothesis: after a move that is large relative to the session's own realised
range so far, price continues in the same direction, but only when the move is
aligned with the session's net direction and supported by aggressive order flow.
We measure "outsized" as the absolute change over the last `lookback` bars
exceeding a fraction (`threshold_mult`) of the session's high-low range so far.
We require the move direction to match the sign of the session's net change from
open (strict alignment), and we require the cumulative delta over the lookback
bars to have the same sign as the move (order-flow confirmation). This filters
out counter-trend spikes and moves driven by passive or mixed flow, which are
more likely to reverse. The session range is tracked incrementally from closed
bars, so no lookahead. We require a minimum number of bars and a minimum range
before acting to avoid noise. The bracket offsets are multiples of the
triggering move, capped to avoid absurd levels.
"""

from collections import deque


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        lookback: int = 3,
        threshold_mult: float = 0.5,
        stop_mult: float = 0.75,
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
        self.session_open = None
        self.bars_in_session = 0
        self.recent_closes: deque[float] = deque(maxlen=lookback + 1)
        self.recent_deltas: deque[float] = deque(maxlen=lookback)

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
            self.recent_deltas.clear()

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
        self.recent_deltas.append(bar.delta)

        # Need enough bars and a meaningful range
        if self.bars_in_session < self.min_bars:
            return
        session_range = self.session_high - self.session_low
        if session_range < self.min_range:
            return

        # Need enough closes and deltas to compute the move and cumulative delta
        if len(self.recent_closes) < self.lookback + 1:
            return
        if len(self.recent_deltas) < self.lookback:
            return

        # Compute move over lookback bars
        move = self.recent_closes[-1] - self.recent_closes[0]
        if abs(move) < self.threshold_mult * session_range:
            return

        # Strict directional alignment: only trade in the direction of the session's net change
        session_change = bar.close - self.session_open
        if session_change > 0 and move <= 0:
            return
        if session_change < 0 and move >= 0:
            return
        if session_change == 0:
            return

        # Order-flow confirmation: cumulative delta must agree with move direction
        cumulative_delta = sum(self.recent_deltas)
        if move > 0 and cumulative_delta <= 0:
            return
        if move < 0 and cumulative_delta >= 0:
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
                tag="outsized-aligned-delta-long",
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="outsized-aligned-delta-short",
            )

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
