"""Refined continuation: outsized move with trend and time filters.

Hypothesis: an outsized intra-session move is more likely to continue if it
aligns with the session's cumulative order flow (delta) and occurs before the
final third of the session. We measure "outsized" as the absolute change over
the last `lookback` bars exceeding `threshold_mult` times the session's
high-low range so far. We require the move direction to match the sign of the
session's cumulative delta (if non-zero) and that we are not too close to the
session close. The bracket is sized off the move, capped by the session range.
"""

from collections import deque


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        lookback: int = 4,
        threshold_mult: float = 0.6,
        stop_mult: float = 1.0,
        target_mult: float = 1.5,
        min_bars: int = 5,
        min_range: float = 0.5,
        max_session_fraction: float = 0.7,
        size: int = 1,
    ):
        self.interval = interval
        self.lookback = lookback
        self.threshold_mult = threshold_mult
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.min_bars = min_bars
        self.min_range = min_range
        self.max_session_fraction = max_session_fraction
        self.size = size

        # Session state
        self.session = None
        self.session_high = None
        self.session_low = None
        self.bars_in_session = 0
        self.cumulative_delta = 0.0
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
            self.cumulative_delta = 0.0
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
        self.cumulative_delta += bar.delta
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

        # Time filter: only trade in the first max_session_fraction of session
        elapsed = ctx.ts - ctx.session_start
        total = ctx.session_end - ctx.session_start
        if total <= 0 or elapsed / total > self.max_session_fraction:
            return

        # Compute move over lookback bars
        move = self.recent_closes[-1] - self.recent_closes[0]
        if abs(move) < self.threshold_mult * session_range:
            return

        # Trend filter: move direction should match cumulative delta sign
        if self.cumulative_delta != 0.0:
            if (move > 0 and self.cumulative_delta < 0) or (
                move < 0 and self.cumulative_delta > 0
            ):
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
