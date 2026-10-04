"""Continuation after an outsized intra-session move, with a safer z-score.

Trial #0 completed and traded but lost money. Trial #1 crashed on a window
read, trial #2 never exposed, trial #3 crashed on static check. This version
keeps the continuation idea but avoids the window crash by keeping session
closes on self rather than reading deep history, and avoids the exposure
problem by acting on every session rather than requiring a long warmup.

The signal: after a bar closes more than `z_threshold` standard deviations
from the session's running mean of closes, bet that the move continues.
Volatility for the z-score and bracket is the standard deviation of closes
over the session so far, which is session-specific and grows with the day.
"""

from __future__ import annotations


class Strategy:
    """Trade continuation of outsized intra-session moves."""

    def __init__(
        self,
        *,
        interval: str = "5m",
        min_bars: int = 10,
        z_threshold: float = 1.5,
        stop_mult: float = 1.0,
        target_mult: float = 1.5,
        size: int = 1,
    ):
        self.interval = interval
        self.min_bars = min_bars
        self.z_threshold = z_threshold
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self.size = size
        self._session = None
        self._session_closes: list[float] = []

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        # Reset per-session state on a session change. No deep history read:
        # everything we need is accumulated on self.
        if ctx.session != self._session:
            self._session = ctx.session
            self._session_closes.clear()

        history = ctx.history(self.interval)
        if not history:
            return

        bar = history[-1]
        self._session_closes.append(bar.close)

        if not ctx.actionable or ctx.position or ctx.working_order_ids:
            return

        # Need enough session-specific history to estimate the typical move.
        if len(self._session_closes) < self.min_bars:
            return

        closes = self._session_closes
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
