"""Short-side opening reversal strategy for NQ futures, v4.

Prior trials show a consistent negative score. The best score so far was
-0.0299 on trial #11, with 333 trades. The original paper found short-side
reversals strongest in the earliest session minutes, with a 65.56% win rate
for the earliest trades.

This version makes several changes to improve edge:
1. Uses a very short opening window (15 minutes) to focus on the earliest
   session action.
2. Uses a faster 15-second interval for more precise signal detection.
3. Requires a strong reversal signal: the current bar must close above the
   prior lookback high AND the bar must be an up bar with a body larger than
   the median body, indicating genuine upward thrust.
4. Adds a minimum range filter to avoid trading in very quiet sessions.
5. Uses a fixed risk size of 1.0 point for a symmetric 1:1 stop/target,
   simpler than ATR-based sizing, to reduce parameter sensitivity.
6. Trades at most once per session.

The ring buffer approach is retained to avoid WindowTooShort errors.
"""

from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "15s",
        opening_minutes: int = 15,
        lookback: int = 8,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.lookback = lookback
        self.size = size
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
            median_body = sorted(self._recent_bodies)[len(self._recent_bodies) // 2]
            avg_range = sum(self._recent_ranges) / len(self._recent_ranges)

            body = abs(last.close - last.open)
            # Signal: close above prior high, up bar, body larger than median,
            # and range is healthy.
            if (
                last.close > prior_high
                and last.close > last.open
                and body > median_body
                and avg_range > 0.25
            ):
                risk = 1.0  # Fixed symmetric 1:1 risk/reward
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
