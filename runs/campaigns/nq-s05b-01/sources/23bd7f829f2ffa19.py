"""Short-side opening reversal strategy for NQ futures, v2.

Prior trials used a lookback of 5 bars and required the signal bar to close
above that lookback high. Trial #3 crashed because it iterated over a slice
longer than the history window. This version avoids that by using the
bounded `last(k)` accessor and by keeping a small ring buffer of recent
highs on `self` rather than slicing history.

The strategy remains short-only, enters during the opening window, and uses
a symmetric 1:1 stop and target. The signal is a bar that closes above the
highest high of the prior `lookback` bars and closes up. We take at most
one trade per session.
"""

from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "1m",
        opening_minutes: int = 60,
        lookback: int = 5,
        risk_points: float = 3.0,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.lookback = lookback
        self.risk_points = risk_points
        self.size = size
        self._session = None
        self._traded_this_session = False
        self._recent_highs: deque[float] = deque(maxlen=lookback)

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        if ctx.position or ctx.working_order_ids:
            return

        # Reset per-session state on session change.
        if ctx.session != self._session:
            self._session = ctx.session
            self._traded_this_session = False
            self._recent_highs.clear()

        if self._traded_this_session:
            return

        # Only trade during the opening window.
        session_elapsed = (ctx.ts - ctx.session_start) / 60_000_000_000
        if session_elapsed > self.opening_minutes:
            return

        if self.interval not in ctx.bars:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        last = history[-1]

        # Maintain the ring buffer of prior highs.
        if len(self._recent_highs) == self.lookback:
            prior_high = max(self._recent_highs)
            # Signal: current bar closes above the prior lookback high, and closes up.
            if last.close > prior_high and last.close > last.open:
                ctx.sell(
                    size=self.size,
                    stop_offset=self.risk_points,
                    target_offset=self.risk_points,
                    tag="opening-reversal-short",
                )
                self._traded_this_session = True
                return

        self._recent_highs.append(last.high)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
