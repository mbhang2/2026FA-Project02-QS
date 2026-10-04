"""Short-side opening reversal strategy for NQ futures, v7.

The campaign requires a short-only reversal strategy using a symmetric 1:1
risk/reward. Prior trial #39 achieved +0.1252 with 359 trades. This version
builds on that approach with refinements intended to improve selectivity and
reduce the number of losing trades.

Key changes from the current strategy:
1. Uses a 30-second bar interval for finer granularity within the opening window.
2. Defines the opening high as the highest high of the first 10 minutes,
   and enters when price closes below that high after making a new high.
3. Requires the reversal bar to close below the previous bar's low, confirming
   downside momentum rather than just a weak close.
4. Uses a fixed risk of 2.0 points and a minimum opening range of 3.0 points
   to filter out quiet sessions while keeping the structure simple.
5. Only trades if the opening high is at least 2.0 points above the first
   bar's close, ensuring a meaningful rally to reverse.
6. Stops trading after 15 minutes from the session open.
"""


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "30s",
        opening_minutes: int = 15,
        high_lookback_minutes: int = 10,
        risk_points: float = 2.0,
        min_range: float = 3.0,
        min_rally: float = 2.0,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.high_lookback_minutes = high_lookback_minutes
        self.risk_points = risk_points
        self.min_range = min_range
        self.min_rally = min_rally
        self.size = size

        self._session = None
        self._traded_this_session = False
        self._opening_high = None
        self._opening_low = None
        self._first_close = None
        self._prev_low = None

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
            self._first_close = None
            self._prev_low = None

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

        # Track the first bar's close.
        if self._first_close is None:
            self._first_close = last.close

        # Track opening high/low only within the high_lookback window.
        if session_elapsed <= self.high_lookback_minutes:
            if self._opening_high is None or last.high > self._opening_high:
                self._opening_high = last.high
            if self._opening_low is None or last.low < self._opening_low:
                self._opening_low = last.low

        # Need an opening high and a previous bar low.
        if self._opening_high is None or self._prev_low is None:
            self._prev_low = last.low
            return

        # Opening range filter.
        opening_range = self._opening_high - self._opening_low
        if opening_range < self.min_range:
            self._prev_low = last.low
            return

        # Rally filter: opening high must be meaningfully above first close.
        rally = self._opening_high - self._first_close
        if rally < self.min_rally:
            self._prev_low = last.low
            return

        # Reversal confirmation: close below previous bar's low.
        if last.close >= self._prev_low:
            self._prev_low = last.low
            return

        # Enter short with fixed symmetric risk.
        ctx.sell(
            size=self.size,
            stop_offset=self.risk_points,
            target_offset=self.risk_points,
            tag="opening-reversal-short",
        )
        self._traded_this_session = True

        # Update prev_low for next bar even though we won't trade again.
        self._prev_low = last.low

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
