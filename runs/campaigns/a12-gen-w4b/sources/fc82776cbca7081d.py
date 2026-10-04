"""Continuation of outsized intra-session moves, session-relative measurement.

Prior trial #0 scored -0.0821 with 7583 trades, suggesting the signal exists
but the execution or threshold was too loose. This version tightens the
threshold, requires confirmation from the bar's own range, and adds a minimum
session-bar count to ensure the z-score is meaningful. It also avoids the
WindowTooShort crash from trial #1 by keeping session closes on self rather
than re-reading deep history.

The core hypothesis: after a bar closes far from the session's running mean,
price continues in that direction. The bracket is sized from the session's
own volatility, so the stop/target adapt to the session's character.
"""

from __future__ import annotations


class Strategy:
    """Trade continuation of outsized intra-session moves."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        lookback: int = 20,
        z_threshold: float = 2.5,
        stop_mult: float = 1.0,
        target_mult: float = 1.5,
        size: int = 1,
    ):
        self.interval = interval
        self.lookback = lookback
        self.z_threshold = z_threshold
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size
        self._session = None
        self._bars_this_session = 0
        self._session_closes: list[float] = []

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if ctx.session != self._session:
            self._session = ctx.session
            self._bars_this_session = 0
            self._session_closes.clear()

        history = ctx.history(self.interval)
        if not history:
            return

        bar = history[-1]
        self._bars_this_session += 1
        self._session_closes.append(bar.close)

        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return

        # Need enough session-specific history to estimate the typical move.
        if self._bars_this_session < self.lookback:
            return

        closes = self._session_closes[-self.lookback:]
        mean = sum(closes) / len(closes)
        variance = sum((c - mean) ** 2 for c in closes) / len(closes)
        std = variance ** 0.5
        if std <= 0:
            return

        # Require the bar itself to have a meaningful range, not just a close
        # far from the mean. This filters out bars where the close is an outlier
        # but the bar traded in a tiny range (e.g., a single print at the end).
        bar_range = bar.high - bar.low
        if bar_range < std * 0.5:
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
