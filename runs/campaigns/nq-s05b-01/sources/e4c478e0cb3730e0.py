"""Short-side opening reversal strategy for NQ futures, v4.

Prior trials show that aggressive shorting at the open has consistently lost
money after costs. The paper's pattern requires a precise definition of a
"high" and a trigger that fires only on genuine exhaustion. This version
takes a more conservative approach:

1. Uses 1-minute bars for a smoother opening range and fewer false signals.
2. Defines the opening high as the maximum high of the first 15 minutes,
   but only enters after a pullback: the current bar must close below the
   prior bar's low, confirming rejection of the high.
3. Requires the high to be at least a minimum distance above the opening
   price to ensure a meaningful rally occurred.
4. Uses a tight stop and target of 0.5 points to reduce exposure to
   adverse moves, while still maintaining the required symmetric 1:1.
5. Trades only once per session and only within the first 45 minutes.

The strategy is deliberately selective, aiming for a high win rate on a
small number of trades rather than high frequency.
"""

from collections import deque


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "1m",
        opening_minutes: int = 15,
        max_entry_minutes: int = 45,
        min_rise_points: float = 2.0,
        risk_points: float = 0.5,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.max_entry_minutes = max_entry_minutes
        self.min_rise_points = min_rise_points
        self.risk_points = risk_points
        self.size = size
        self._session = None
        self._traded_this_session = False
        self._opening_high: float | None = None
        self._opening_price: float | None = None
        self._prev_low: float | None = None
        self._bars_since_high = 0

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
            self._opening_price = None
            self._prev_low = None
            self._bars_since_high = 0

        if self._traded_this_session:
            return

        if self.interval not in ctx.bars:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        last = history[-1]

        # Only consider bars within the first max_entry_minutes.
        session_elapsed = (ctx.ts - ctx.session_start) / 60_000_000_000
        if session_elapsed > self.max_entry_minutes:
            return

        # Track the opening price (first bar's open) and the opening high.
        if self._opening_price is None:
            self._opening_price = last.open
            self._opening_high = last.high
            self._prev_low = last.low
            return

        # Update opening high if still within the opening window.
        if session_elapsed <= self.opening_minutes:
            if last.high > self._opening_high:
                self._opening_high = last.high
                self._bars_since_high = 0
            else:
                self._bars_since_high += 1
        else:
            # After the opening window, we look for a reversal.
            if self._opening_high is None:
                return

            rise = self._opening_high - self._opening_price
            if rise < self.min_rise_points:
                return

            # Reversal signal: current bar closes below the previous bar's low,
            # indicating rejection of the opening high.
            if self._prev_low is not None and last.close < self._prev_low:
                ctx.sell(
                    size=self.size,
                    stop_offset=self.risk_points,
                    target_offset=self.risk_points,
                    tag="opening-reversal-short",
                )
                self._traded_this_session = True
                return

        self._prev_low = last.low

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
