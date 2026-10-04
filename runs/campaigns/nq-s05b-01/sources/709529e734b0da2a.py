"""Short-side opening reversal strategy for NQ futures, v7.

This version simplifies the signal and tightens the entry window based on
prior feedback. The best prior scores came from strategies that traded
sparingly and had a high win rate. This version:

1. Uses 1-minute bars for early signal detection.
2. Only trades during the first 10 minutes of the session.
3. Requires a clear opening rally: the high of the first 3 bars must be
   at least 3 points above the first bar's close.
4. Enters short only when a bar closes below the low of the previous bar
   (a confirmed lower close), indicating a reversal has begun.
5. Uses a fixed risk of 2 points for a symmetric 1:1 target/stop.
6. Trades only once per session.
"""


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "1m",
        opening_minutes: int = 10,
        lookback: int = 3,
        min_rally: float = 3.0,
        risk_points: float = 2.0,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.lookback = lookback
        self.min_rally = min_rally
        self.risk_points = risk_points
        self.size = size

        self._session = None
        self._traded_this_session = False
        self._bars_seen = 0
        self._first_close = None
        self._opening_high = None
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
            self._bars_seen = 0
            self._first_close = None
            self._opening_high = None
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
        self._bars_seen += 1

        # Track the first bar's close and previous bar's low.
        if self._bars_seen == 1:
            self._first_close = last.close
            self._opening_high = last.high
            self._prev_low = last.low
            return

        # Need enough bars to establish the opening high.
        if self._bars_seen <= self.lookback:
            if last.high > self._opening_high:
                self._opening_high = last.high
            self._prev_low = last.low
            return

        # Update opening high with the current bar.
        if last.high > self._opening_high:
            self._opening_high = last.high

        # Require a meaningful rally above the first bar's close.
        if self._first_close is not None:
            rally = self._opening_high - self._first_close
            if rally < self.min_rally:
                self._prev_low = last.low
                return

        # Signal: bar closes below the previous bar's low.
        if last.close >= self._prev_low:
            self._prev_low = last.low
            return

        # Enter short with a fixed symmetric risk.
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
