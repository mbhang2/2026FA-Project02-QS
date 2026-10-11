"""Does an outsized move inside a session continue, or give it back?

Fifth proposal. The continuation strategy #1 scored +0.0874, while the
give-back inversion #2 scored only +0.0047. The confirmation variant #3 was
not scored because it was exposed in too few sessions. This version returns
to the continuation hypothesis with a cleaner, more selective signal and a
tighter risk profile.

Key changes from #1:
- The signal is measured over a rolling window of the last 20 bars rather than
  the whole session, so the "realized range so far" adapts more quickly to
  changes in volatility.
- The outsized move is required to be at least 1.5x the average move of the
  prior 20 bars, rather than a percentile of the session's ratios. This is a
  more direct measure of "outsized" that does not require a long warm-up.
- A minimum of 20 bars is required before the first trade, so the rolling
  window is fully populated.
- The bracket is symmetric: stop and target are both 1.5x the rolling range.
  This gives a balanced risk/reward profile.
- The strategy is flat at the session close: all orders are DAY orders and the
  bracket legs expire with the session.
"""

from __future__ import annotations

from src.core.types import Bar


class Strategy:
    """Continuation on outsized intra-session moves, rolling-window version."""

    def __init__(self, *, interval: str = "5m", size: int = 1,
                 lookback: int = 20, min_mult: float = 1.5,
                 stop_mult: float = 1.5, target_mult: float = 1.5):
        self.interval = interval
        self.size = size
        self.lookback = lookback
        self.min_mult = min_mult
        self.stop_mult = stop_mult
        self.target_mult = target_mult
        self._session = None
        self._session_bars: list[Bar] = []

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if ctx.session != self._session:
            self._session = ctx.session
            self._session_bars = []

        history = ctx.history(self.interval)
        if len(history) < 1:
            return
        last = history[-1]
        if last.session != ctx.session:
            return
        self._session_bars.append(last)

        if not ctx.actionable:
            return
        if ctx.position or ctx.working_order_ids:
            return

        bars = self._session_bars
        if len(bars) < self.lookback + 1:
            return

        current = bars[-1]
        if current.close == current.open:
            return

        prior = bars[-(self.lookback + 1):-1]
        high = max(b.high for b in prior)
        low = min(b.low for b in prior)
        rng = high - low
        if rng <= 0:
            return

        # Average absolute close-to-close move over the lookback window.
        avg_move = sum(abs(b.close - b.open) for b in prior) / len(prior)
        if avg_move <= 0:
            return

        move = abs(current.close - current.open)
        if move < self.min_mult * avg_move:
            return

        direction = 1 if current.close > current.open else -1
        stop = rng * self.stop_mult
        target = rng * self.target_mult

        if direction > 0:
            ctx.buy(size=self.size, stop_offset=stop, target_offset=target,
                    tag="outsized-rolling-long")
        else:
            ctx.sell(size=self.size, stop_offset=stop, target_offset=target,
                     tag="outsized-rolling-short")

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
