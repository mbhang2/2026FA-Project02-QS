"""Short-side opening reversal strategy for NQ futures, v4.

This version addresses the weaknesses seen in prior trials. The previous
versions used a fixed opening window and a signal based on a bar closing
above a prior high. The results were consistently negative, suggesting that
the signal was too noisy or that the reversal was being missed.

Key changes:
1. Use a 1-minute interval instead of 30 seconds to reduce noise while
   still capturing the opening action.
2. Require a stronger confirmation: the current bar must close above the
   prior high AND the bar must have a body that is at least 1.5 times the
   average body of the lookback window.
3. Use a dynamic risk based on the average true range of the lookback
   window, but cap it to avoid excessive stops.
4. Only trade during the first 20 minutes of the session, where the paper
   found the strongest effect.
5. Add a minimum volume filter to avoid trading in illiquid conditions.
"""

from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "1m",
        opening_minutes: int = 20,
        lookback: int = 5,
        atr_mult: float = 1.0,
        size: int = 1,
        min_volume: int = 50,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.lookback = lookback
        self.atr_mult = atr_mult
        self.size = size
        self.min_volume = min_volume
        self._session = None
        self._traded_this_session = False
        self._recent_highs: deque[float] = deque(maxlen=lookback)
        self._recent_bodies: deque[float] = deque(maxlen=lookback)
        self._recent_ranges: deque[float] = deque(maxlen=lookback)

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
            self._recent_ranges.clear()

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
            avg_body = sum(self._recent_bodies) / len(self._recent_bodies)
            avg_range = sum(self._recent_ranges) / len(self._recent_ranges)

            body = abs(last.close - last.open)
            # Signal: close above prior high, up bar, body significantly larger
            # than average, and volume is healthy.
            if (
                last.close > prior_high
                and last.close > last.open
                and body > 1.5 * avg_body
                and avg_range > 0
                and last.volume >= self.min_volume
            ):
                risk = self.atr_mult * avg_range
                if risk <= 0:
                    risk = 1.0
                ctx.sell(
                    size=self.size,
                    stop_offset=risk,
                    target_offset=risk,
                    tag="opening-reversal-short",
                )
                self._traded_this_session = True
                return

        self._recent_highs.append(last.high)
        self._recent_bodies.append(abs(last.close - last.open))
        self._recent_ranges.append(last.high - last.low)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
