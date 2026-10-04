"""Short-side opening reversal strategy for NQ futures, v9.

This version fixes the static-check error from v8 by explicitly handling
the None case for opening_high and opening_low. The strategy:

1. Uses 1-minute bars for the opening window.
2. Tracks the opening high over the first 5 bars.
3. Enters short when a bar closes below the prior bar's low after a new
   opening high was established, within the first 15 minutes.
4. Uses a symmetric 1:1 risk/reward of 1.5 points.
5. Requires a minimum opening range of 2.0 points.
"""


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "1m",
        opening_minutes: int = 15,
        lookback_bars: int = 5,
        min_range: float = 2.0,
        risk: float = 1.5,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.lookback_bars = lookback_bars
        self.min_range = min_range
        self.risk = risk
        self.size = size

        self._session = None
        self._traded_this_session = False
        self._bars_seen = 0
        self._opening_high = None
        self._opening_low = None

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
            self._opening_high = None
            self._opening_low = None

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

        # Track opening high and low.
        if self._opening_high is None or self._opening_low is None:
            self._opening_high = last.high
            self._opening_low = last.low
        else:
            if last.high > self._opening_high:
                self._opening_high = last.high
            if last.low < self._opening_low:
                self._opening_low = last.low

        # Need enough bars to establish an opening range.
        if self._bars_seen < self.lookback_bars:
            return

        # Explicit None checks for static type checker.
        if self._opening_high is None or self._opening_low is None:
            return

        # Require a meaningful opening range.
        opening_range = self._opening_high - self._opening_low
        if opening_range < self.min_range:
            return

        # Need at least two bars for the reversal signal.
        if len(history) < 2:
            return

        prior = history[-2]

        # Reversal signal: current bar closes below the prior bar's low,
        # and the prior bar made a new opening high.
        if last.close >= prior.low:
            return
        if prior.high < self._opening_high:
            return

        # Require a bearish bar: close below open.
        if last.close >= last.open:
            return

        # Enter short with symmetric risk/reward.
        ctx.sell(
            size=self.size,
            stop_offset=self.risk,
            target_offset=self.risk,
            tag="opening-reversal-short",
        )
        self._traded_this_session = True

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
