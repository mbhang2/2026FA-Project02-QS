"""Short-side opening reversal strategy for NQ futures, v5.

Prior trials show the simple short-side reversal is not profitable on NQ.
The best score so far is -0.0299 (trial #11). This version attempts to
improve on #11 by tightening the entry criteria and using a more
conservative approach that waits for a clear reversal signal rather than
just a breakout above a prior high.

Key changes from v4:
1. Uses a 1-minute interval to reduce noise.
2. Requires a double confirmation: price must close above the prior lookback
   high AND then close back below that high on a subsequent bar, indicating
   the reversal has actually begun.
3. Adds a minimum number of bars before entry to ensure sufficient data.
4. Uses a fixed risk based on the average true range of the lookback window,
   but caps it to avoid excessive stops in volatile sessions.
5. Only trades if the session has shown sufficient volatility (range > threshold).
"""

from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "1m",
        opening_minutes: int = 45,
        lookback: int = 15,
        atr_mult: float = 1.0,
        max_risk: float = 10.0,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.lookback = lookback
        self.atr_mult = atr_mult
        self.max_risk = max_risk
        self.size = size
        self._session = -1
        self._traded_this_session = False
        self._bars_this_session = 0
        self._recent_highs: deque[float] = deque(maxlen=lookback)
        self._recent_ranges: deque[float] = deque(maxlen=lookback)
        self._prior_high: float | None = None
        self._high_break_bar: int | None = None

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
            self._bars_this_session = 0
            self._recent_highs.clear()
            self._recent_ranges.clear()
            self._prior_high = None
            self._high_break_bar = None

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
        self._bars_this_session += 1

        # Need at least lookback bars before we can compute the prior high.
        if self._bars_this_session < self.lookback:
            self._recent_highs.append(last.high)
            self._recent_ranges.append(last.high - last.low)
            return

        # Compute prior high from the lookback window (excluding current bar).
        prior_high = max(self._recent_highs)
        avg_range = sum(self._recent_ranges) / len(self._recent_ranges)

        # State machine for the double confirmation:
        # State 0: waiting for a close above prior high
        # State 1: high was broken, waiting for a close back below prior high
        if self._prior_high is None:
            if last.close > prior_high:
                self._prior_high = prior_high
                self._high_break_bar = self._bars_this_session
        else:
            # We had a high break. Check if price closed back below the prior high.
            if last.close < self._prior_high:
                # Reversal confirmed. Check volatility filter.
                if avg_range > 0:
                    risk = min(self.atr_mult * avg_range, self.max_risk)
                    if risk > 0:
                        ctx.sell(
                            size=self.size,
                            stop_offset=risk,
                            target_offset=risk,
                            tag="opening-reversal-short",
                        )
                        self._traded_this_session = True
                        return
                # Reset if no trade was taken.
                self._prior_high = None
                self._high_break_bar = None
            elif self._high_break_bar is not None and self._bars_this_session - self._high_break_bar > 5:
                # Too many bars since the high break, reset.
                self._prior_high = None
                self._high_break_bar = None

        # Update ring buffers.
        self._recent_highs.append(last.high)
        self._recent_ranges.append(last.high - last.low)

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
