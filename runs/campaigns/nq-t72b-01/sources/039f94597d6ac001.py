"""Investigate whether an outsized intra-session move continues, using ATR.

Hypothesis: after a move that is large relative to the session's recent
volatility (measured by average true range), price continues in the same
direction. This refines the prior "outsized" measure: instead of comparing the
move to the session's total high-low range, we compare it to the average true
range over the last `atr_lookback` bars. A move that is large relative to
recent volatility is more likely to represent a genuine information event
rather than just a wide-ranging session.

We track session state incrementally: session high/low, recent closes, and a
rolling average of true ranges. The trigger requires the absolute move over
`lookback` bars to exceed `threshold_mult` times the current ATR. We enter in
the direction of the move with a protective bracket sized off the move itself,
capped to avoid absurd levels.

This strategy is a direct variation of the best prior trial (#0), replacing the
session-range denominator with ATR. It is expected to produce a similar number
of trades while potentially improving signal quality by focusing on moves that
are unusual given recent volatility.
"""

from collections import deque


class Strategy:
    def __init__(
        self,
        interval: str = "5m",
        lookback: int = 3,
        threshold_mult: float = 1.0,
        atr_lookback: int = 20,
        stop_mult: float = 1.0,
        target_mult: float = 1.5,
        min_bars: int = 25,
        min_atr: float = 0.1,
        size: int = 1,
    ):
        self.interval = interval
        self.lookback = lookback
        self.threshold_mult = threshold_mult
        self.atr_lookback = atr_lookback
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.min_bars = min_bars
        self.min_atr = min_atr
        self.size = size

        # Session state
        self.session = None
        self.session_high = None
        self.session_low = None
        self.bars_in_session = 0
        self.recent_closes: deque[float] = deque(maxlen=lookback + 1)
        self.true_ranges: deque[float] = deque(maxlen=atr_lookback)
        self.prev_close = None

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
            self.bars_in_session = 0
            self.recent_closes.clear()
            self.true_ranges.clear()
            self.prev_close = None

        history = ctx.history(self.interval)
        if len(history) == 0:
            return
        bar = history[-1]  # the bar that just closed

        self.bars_in_session += 1
        if self.session_high is None or bar.high > self.session_high:
            self.session_high = bar.high
        if self.session_low is None or bar.low < self.session_low:
            self.session_low = bar.low

        # Update true range
        if self.prev_close is not None:
            tr = max(
                bar.high - bar.low,
                abs(bar.high - self.prev_close),
                abs(bar.low - self.prev_close),
            )
            self.true_ranges.append(tr)
        self.prev_close = bar.close

        self.recent_closes.append(bar.close)

        # Need enough bars and a meaningful ATR
        if self.bars_in_session < self.min_bars:
            return
        if len(self.true_ranges) < self.atr_lookback:
            return
        atr = sum(self.true_ranges) / len(self.true_ranges)
        if atr < self.min_atr:
            return

        # Need enough closes to compute the move
        if len(self.recent_closes) < self.lookback + 1:
            return

        # Compute move over lookback bars
        move = self.recent_closes[-1] - self.recent_closes[0]
        if abs(move) < self.threshold_mult * atr:
            return

        # Already in a position or have working orders
        if ctx.position or ctx.working_order_ids:
            return

        # Determine direction and place order with bracket
        stop_offset = min(self.stop_mult * abs(move), atr * 3.0)
        target_offset = min(self.target_mult * abs(move), atr * 5.0)
        if move > 0:
            ctx.buy(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="outsized-atr-continuation-long",
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop_offset,
                target_offset=target_offset,
                tag="outsized-atr-continuation-short",
            )

    def on_fill(self, ctx):
        pass

    def on_lifecycle(self, ctx):
        pass
