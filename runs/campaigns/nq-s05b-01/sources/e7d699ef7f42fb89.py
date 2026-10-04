"""Short-side opening reversal strategy for NQ futures, v5.

The campaign requires a short-only reversal strategy using a symmetric 1:1
risk/reward. Prior trials show a wide range of outcomes, with the best positive
score at +0.0535. This version attempts to isolate the most promising regime by:

1. Restricting trades to the first 10 minutes of the session, tighter than
   previous attempts.
2. Using a 1-minute bar interval for early granularity.
3. Defining the opening high as the highest high of the first 3 bars.
4. Entering short only when a bar closes below the opening high after having
   made a new high in that bar, and the bar is a bearish candle (close < open).
5. Using a fixed risk based on the opening range, with a minimum distance to
   avoid noise.
6. Trading only once per session.

The rationale is to capture the reversal that appears immediately after the
initial push, while avoiding later, noisier periods. The 1:1 risk/reward keeps
the structure symmetric as required.
"""


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "1m",
        opening_minutes: int = 10,
        lookback: int = 3,
        risk_mult: float = 0.4,
        min_risk: float = 0.25,
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

        # Signal: price made a new opening high and then closed below it,
        # with a bearish candle.
        if last.close < self._opening_high and last.close < last.open:
            # Risk is a fraction of the distance from the high to the close,
            # with a minimum to avoid tiny stops.
            risk = self.risk_mult * (self._opening_high - last.close)
            if risk < self.min_risk:
                risk = self.min_risk
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
