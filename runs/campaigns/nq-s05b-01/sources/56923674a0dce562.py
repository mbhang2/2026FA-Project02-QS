"""Short-side opening reversal strategy for NQ futures, v4.

Prior trials show the opening reversal short is not profitable with the
current formulation. The best score so far is -0.0299 (trial #11), which
used a 30-second interval, a 30-minute window, and an ATR-based risk.

This version attempts to improve on that by:
1. Using a 1-minute interval to reduce noise while still capturing the
   opening reversal.
2. Requiring a stronger breakout: the close must be above the prior
   lookback high AND the bar must be an up bar with a body larger than
   the median body, AND the bar must close in the upper half of its range.
3. Adding a volume filter: the breakout bar's volume must be above the
   median volume of the lookback window, indicating genuine participation.
4. Using a fixed risk of 2.0 points instead of ATR-based risk, since the
   ATR-based risk may have been too tight or too loose. The symmetric 1:1
   structure is maintained.
5. Reducing the lookback to 8 bars to be more responsive to recent price
   action.
"""

from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "1m",
        opening_minutes: int = 30,
        lookback: int = 8,
        risk_points: float = 2.0,
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
        self._recent_bodies: deque[float] = deque(maxlen=lookback)
        self._recent_volumes: deque[int] = deque(maxlen=lookback)

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
            self._recent_bodies.clear()
            self._recent_volumes.clear()

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

        # Update ring buffers with prior bars.
        if len(self._recent_highs) == self.lookback:
            prior_high = max(self._recent_highs)
            median_body = sorted(self._recent_bodies)[len(self._recent_bodies) // 2]
            median_volume = sorted(self._recent_volumes)[len(self._recent_volumes) // 2]

            body = abs(last.close - last.open)
            bar_range = last.high - last.low
            upper_half_close = last.close >= last.low + 0.5 * bar_range

            # Signal: close above prior high, up bar, body larger than median,
            # close in upper half, and volume above median.
            if (
                last.close > prior_high
                and last.close > last.open
                and body > median_body
                and upper_half_close
                and last.volume > median_volume
                and bar_range > 0
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
        self._recent_bodies.append(abs(last.close - last.open))
        self._recent_volumes.append(last.volume)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
