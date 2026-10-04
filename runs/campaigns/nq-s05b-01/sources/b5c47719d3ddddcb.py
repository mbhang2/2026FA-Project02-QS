"""Short-only opening reversal strategy for NQ futures, v5.

The campaign requires a short-only reversal strategy using a symmetric 1:1
risk/reward. Prior trials were mostly negative; the best score was +0.0535
(#22). This version attempts to improve on that by:

1. Defining the opening high as the highest high of the first 5 minutes,
   not a rolling lookback.
2. Entering short only when price closes below that opening high AND the
   bar is bearish, within the first 30 minutes.
3. Using a fixed risk based on the opening range (high - low) rather than
   the distance from high to close, which was too small.
4. Adding a minimum risk threshold to avoid over-trading on tiny ranges.
5. Trading at most once per session with a 1-minute bar interval.
"""


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "1m",
        opening_minutes: int = 30,
        high_window_minutes: int = 5,
        risk_mult: float = 0.5,
        min_risk: float = 0.25,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.high_window_minutes = high_window_minutes
        self.risk_mult = risk_mult
        self.min_risk = min_risk
        self.size = size
        self._session = None
        self._traded_this_session = False
        self._opening_high = None
        self._opening_low = None
        self._bars_seen = 0

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
            self._opening_low = None
            self._bars_seen = 0

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
        self._bars_seen += 1

        # Establish opening high/low during the first high_window_minutes.
        if session_elapsed <= self.high_window_minutes:
            if self._opening_high is None or last.high > self._opening_high:
                self._opening_high = last.high
            if self._opening_low is None or last.low < self._opening_low:
                self._opening_low = last.low
            return

        # After the high window, require a bearish close below the opening high.
        if self._opening_high is None or self._opening_low is None:
            return

        if last.close < self._opening_high and last.close < last.open:
            opening_range = self._opening_high - self._opening_low
            risk = self.risk_mult * opening_range
            if risk < self.min_risk:
                return
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
