"""Short-side opening reversal strategy for NQ futures, v5.

The campaign requires a short-only reversal strategy using a symmetric 1:1
risk/reward. Prior trials have been mostly negative, with the best score
around +0.0535. This version attempts to improve by:

1. Restricting trades to the first 15 minutes of the session.
2. Using a 1-minute bar interval.
3. Defining the opening high as the highest high of the first N bars.
4. Entering short when price closes below the opening high after having
   made a new high, but requiring the close to be below the prior bar's
   low to confirm a stronger reversal.
5. Using a fixed risk based on the opening range, with a minimum risk
   to avoid overly tight stops.
6. Only trading once per session.
"""


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "1m",
        opening_minutes: int = 15,
        lookback: int = 5,
        risk_mult: float = 0.5,
        min_risk: float = 0.5,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.lookback = lookback
        self.risk_mult = risk_mult
        self.min_risk = min_risk
        self.size = size
        self._session = None
        self._traded_this_session = False
        self._opening_high = None
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

        # Need enough bars to establish an opening high.
        if self._bars_seen < self.lookback:
            if self._opening_high is None or last.high > self._opening_high:
                self._opening_high = last.high
            return

        # Update opening high with the current bar.
        if last.high > self._opening_high:
            self._opening_high = last.high

        # Signal: price made a new opening high and then closed below the
        # prior bar's low, confirming a reversal.
        if self._bars_seen >= 2:
            prior = history[-2]
            if last.close < self._opening_high and last.close < prior.low:
                # Risk is a fraction of the distance from the high to the close.
                risk = self.risk_mult * (self._opening_high - last.close)
                risk = max(risk, self.min_risk)
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
