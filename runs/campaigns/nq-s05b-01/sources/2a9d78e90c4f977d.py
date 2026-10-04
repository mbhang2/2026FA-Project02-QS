"""Short-side opening reversal strategy for NQ futures, v7.

This version simplifies the entry logic to increase trade frequency while
maintaining the core reversal idea. The campaign requires a short-only
reversal strategy with symmetric 1:1 risk/reward. Prior trials with many
filters often traded too infrequently and were not scored due to the 70%
exposure floor. The best scores came from strategies that traded in over 90%
of sessions. This version aims to trade in most sessions by using a simple
opening-range breakout reversal: after the first `lookback` minutes, if price
closes below the highest high of those bars, we sell with a fixed stop and
target distance. A minimum rally above the first bar's close ensures a
meaningful upward move to reverse. The risk distance is fixed in points to
keep the structure symmetric and simple.
"""


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "1m",
        lookback: int = 5,
        risk_points: float = 2.0,
        min_rally: float = 0.5,
        max_elapsed_minutes: int = 30,
        size: int = 1,
    ):
        self.interval = interval
        self.lookback = lookback
        self.risk_points = risk_points
        self.min_rally = min_rally
        self.max_elapsed_minutes = max_elapsed_minutes
        self.size = size

        self._session = None
        self._traded_this_session = False
        self._opening_high = None
        self._first_close = None
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
            self._first_close = None
            self._bars_seen = 0

        if self._traded_this_session:
            return

        # Only trade within the opening window.
        session_elapsed = (ctx.ts - ctx.session_start) / 60_000_000_000
        if session_elapsed > self.max_elapsed_minutes:
            return

        if self.interval not in ctx.bars:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        last = history[-1]
        self._bars_seen += 1

        # Track the first bar's close for the rally filter.
        if self._bars_seen == 1:
            self._first_close = last.close

        # Update opening high with the current bar's high.
        if self._opening_high is None or last.high > self._opening_high:
            self._opening_high = last.high

        # Need at least lookback bars to establish the opening high.
        if self._bars_seen < self.lookback:
            return

        # Require a meaningful rally above the first bar's close.
        if self._first_close is not None:
            rally = self._opening_high - self._first_close
            if rally < self.min_rally:
                return

        # Signal: price closed below the opening high.
        if last.close >= self._opening_high:
            return

        # Enter short with symmetric stop and target.
        ctx.sell(
            size=self.size,
            stop_offset=self.risk_points,
            target_offset=self.risk_points,
            tag="opening-reversal-short",
        )
        self._traded_this_session = True

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
