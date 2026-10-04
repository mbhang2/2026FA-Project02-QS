"""Investigate whether an outsized move inside a session continues or gives back.

This version fixes the window issue from trial #1 by maintaining our own
session history on self instead of relying on ctx.history's bounded window.
It also adds a minimum trade count to avoid acting on sessions with too few
bars to be meaningful.

The idea remains: measure the session's own realized range so far via the
average true range of completed 5m bars, then detect a bar whose close is far
from the session's running mean. Take a position in the direction of the move,
with a bracket sized from the same session-specific volatility. This is a
continuation bet: the outsized move has further to run.

Every parameter has a default; the class is constructible with no arguments.
"""

from __future__ import annotations


class Strategy:
    """Trade continuation of outsized intra-session moves."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 20,
        z_threshold: float = 2.0,
        stop_mult: float = 1.0,
        target_mult: float = 1.5,
        size: int = 1,
        min_bars: int = 30,
    ):
        self.interval = interval
        self.lookback = lookback
        self.z_threshold = z_threshold
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size
        self.min_bars = min_bars
        self._session = None
        self._bars_this_session = 0
        self._session_bars: list[float] = []

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        # Session boundary: reset our own accumulated history.
        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_this_session = 0
            self._session_bars.clear()

        # We only act on the bar that just closed for our interval.
        if self.interval not in ctx.bars:
            return

        history = ctx.history(self.interval)
        if not history:
            return

        bar = history[-1]
        self._bars_this_session += 1
        self._session_bars.append(bar.close)

        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return

        # Need enough session-specific history to estimate the typical move.
        if self._bars_this_session < self.lookback:
            return

        # Also require a minimum number of bars to ensure the session has
        # enough activity for a meaningful z-score.
        if self._bars_this_session < self.min_bars:
            return

        # Use our own accumulated session closes, not ctx.history, to avoid
        # the bounded window problem.
        closes = self._session_bars[-self.lookback:]
        mean = sum(closes) / len(closes)
        variance = sum((c - mean) ** 2 for c in closes) / len(closes)
        std = variance ** 0.5
        if std <= 0:
            return

        z = (bar.close - mean) / std
        if abs(z) < self.z_threshold:
            return

        direction = 1 if z > 0 else -1
        stop_dist = std * self.stop_mult
        target_dist = std * self.target_mult

        if direction > 0:
            ctx.buy(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="continuation-long",
            )
        else:
            ctx.sell(
                size=self.size,
                stop_offset=stop_dist,
                target_offset=target_dist,
                tag="continuation-short",
            )

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
