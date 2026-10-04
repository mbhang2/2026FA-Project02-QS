"""Short-side opening reversal strategy for NQ futures, v5.

Prior trials show the previous versions were not profitable, with scores
ranging from -0.29 to -0.03. The best score (-0.0299) came from trial #11,
which used a 30-minute opening window, 30-second bars, and a signal based
on a close above the prior lookback high with a body larger than the median.

This version makes a more aggressive change: instead of a single fixed
lookback, it uses a dynamic lookback that adapts to the number of bars seen
so far in the opening window. The idea is that early in the session, only a
few bars are available, so the lookback should be shorter; later, more bars
are available for context. The signal remains a close above the prior high,
but we also require the close to be in the upper half of the bar's range,
which indicates a stronger upward push. The stop/target distance is based
on the average true range of the lookback window, scaled by a factor.

We also add a minimum number of bars before trading to avoid acting on
insufficient data.
"""

from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "30s",
        opening_minutes: int = 30,
        min_bars: int = 3,
        max_lookback: int = 20,
        atr_mult: float = 1.5,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.min_bars = min_bars
        self.max_lookback = max_lookback
        self.atr_mult = atr_mult
        self.size = size
        self._session = None
        self._traded_this_session = False
        # Store recent bars as tuples (high, low, close, open, range)
        self._recent_bars: deque[tuple[float, float, float, float, float]] = deque(maxlen=max_lookback)

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
            self._recent_bars.clear()

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
        bar_range = last.high - last.low
        if bar_range <= 0:
            return

        # Add current bar to recent bars.
        self._recent_bars.append((last.high, last.low, last.close, last.open, bar_range))

        # Need at least min_bars before we can compute a signal.
        if len(self._recent_bars) < self.min_bars:
            return

        # Dynamic lookback: use all bars seen so far in this session, up to max_lookback.
        lookback_bars = list(self._recent_bars)
        # Exclude the current bar from the lookback when computing prior high.
        prior_bars = lookback_bars[:-1]
        if not prior_bars:
            return

        prior_high = max(b[0] for b in prior_bars)
        # Average true range approximation using high-low range.
        avg_range = sum(b[4] for b in prior_bars) / len(prior_bars)

        # Signal: current close is above the prior high, and the close is in the
        # upper half of the current bar's range (strong upward push).
        close_position = (last.close - last.low) / bar_range
        if last.close > prior_high and close_position > 0.5:
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

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
