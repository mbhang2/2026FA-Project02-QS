"""Short-side opening reversal strategy for NQ futures, v3.

The previous version entered on a bar that closed above the prior lookback high
and closed up. It produced many trades (383-384) and consistently negative
scores, suggesting the signal was too noisy and the opening window too wide.

This version tightens the reversal definition substantially:
- The opening window is shortened to 30 minutes.
- The signal requires a bar to close above the highest high of the prior 10
  bars, not 5, and to close in the upper half of its range.
- We require the bar's close to be at least 0.5 index points above the prior
  high, filtering out marginal breakouts.
- We also require the bar's range to be meaningful (>= 1.0 point), so we are
  not trading on tiny, noisy bars.
- The stop and target are symmetric at 2.5 points, tighter than before, which
  may improve the win rate on a reversal signal.
- We still take at most one trade per session.

The goal is to reduce the number of trades and focus on stronger opening
reversals, which the paper suggests have the highest win rate.
"""

from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "1m",
        opening_minutes: int = 30,
        lookback: int = 10,
        risk_points: float = 2.5,
        min_range_points: float = 1.0,
        breakout_points: float = 0.5,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.lookback = lookback
        self.risk_points = risk_points
        self.min_range_points = min_range_points
        self.breakout_points = breakout_points
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
            bar_range = last.high - last.low
            # Signal conditions:
            # - close above prior high by at least breakout_points
            # - bar closed up (close > open)
            # - close in upper half of the bar's range
            # - bar range at least min_range_points
            if (
                last.close > prior_high + self.breakout_points
                and last.close > last.open
                and last.close > (last.low + last.high) / 2.0
                and bar_range >= self.min_range_points
            ):
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
