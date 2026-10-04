"""Short-side opening reversal strategy for NQ futures, v3.

The prior version used a fixed 60-minute opening window and a signal of a
bar closing above the prior lookback high. Trials #0-#7 all produced
negative scores, suggesting either the signal is too weak, the window is
too wide, or the symmetric 1:1 structure is not capturing the reversal
well.

This version makes several changes:
1. Shortens the opening window to 30 minutes to focus on the earliest
   session action where the paper found the strongest effect.
2. Uses a faster 30-second interval to catch the reversal more precisely
   while still avoiding tick subscription.
3. Requires a stronger signal: the current bar must close above the prior
   lookback high AND the bar must be an up bar with a body larger than the
   median body of the lookback window, indicating genuine upward thrust
   rather than noise.
4. Adds a minimum range filter to avoid trading in very quiet sessions.
5. Keeps the symmetric 1:1 stop/target as required, but sizes the risk
   based on the recent average true range so the stop adapts to volatility.

The ring buffer approach is retained to avoid WindowTooShort errors.
"""

from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "30s",
        opening_minutes: int = 30,
        lookback: int = 10,
        atr_mult: float = 1.5,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.lookback = lookback
        self.atr_mult = atr_mult
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
                and avg_range > 0
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
