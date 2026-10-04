"""Short-side opening reversal strategy for NQ futures, v6.

Prior trials: #11 had the best score (-0.0299) using a 30-minute opening
window, 30-second bars, and a signal based on a close above the prior
lookback high with a body larger than the median. The previous v5
attempted a dynamic lookback and close position filter but produced
identical synthetic fills and likely no improvement.

This version keeps the proven 30-minute window and 30-second bars, but
changes the signal to focus on a more precise opening-range breakout
reversal: we track the high of the first N bars of the session and only
take a short when price closes above that high after first pulling back
below it. This "false breakout" pattern is a classic reversal setup and
adds a condition that the previous version lacked: we require a prior
pullback below the opening high before the breakout close, which filters
out strong trending moves that never look back.

The stop/target remains symmetric 1:1, sized from the average range of
the lookback window scaled by a factor. We also add a minimum bar count
to ensure enough data exists before trading.
"""

from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "30s",
        opening_minutes: int = 30,
        opening_bars: int = 5,
        atr_mult: float = 1.5,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.opening_bars = opening_bars
        self.atr_mult = atr_mult
        self.size = size
        self._session = None
        self._traded_this_session = False
        self._opening_high: float | None = None
        self._bars_since_open = 0
        self._pulled_back = False
        self._recent_ranges: deque[float] = deque(maxlen=20)

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
            self._opening_high = None
            self._bars_since_open = 0
            self._pulled_back = False
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
        bar_range = last.high - last.low
        if bar_range <= 0:
            return

        self._bars_since_open += 1
        self._recent_ranges.append(bar_range)

        # Set the opening high from the first N bars.
        if self._opening_high is None:
            if self._bars_since_open >= self.opening_bars:
                # Use the max high of the first opening_bars bars.
                # Since we only keep recent ranges, we need to track the high
                # incrementally.
                # We'll compute it from the history window.
                if len(history) >= self.opening_bars:
                    opening_bars = history[-self.opening_bars:]
                    self._opening_high = max(b.high for b in opening_bars)
                else:
                    return
            else:
                return

        # Check for pullback below opening high.
        if last.low < self._opening_high:
            self._pulled_back = True

        # Signal: after a pullback, price closes above the opening high.
        if self._pulled_back and last.close > self._opening_high:
            # Average range of recent bars for risk sizing.
            if len(self._recent_ranges) < 2:
                return
            avg_range = sum(self._recent_ranges) / len(self._recent_ranges)
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
