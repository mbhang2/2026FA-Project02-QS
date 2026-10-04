"""Short-side opening reversal for NQ, v8.

The campaign requires a short-only reversal strategy using a symmetric 1:1
risk/reward. Prior trials have been mostly negative, with the best score
+0.1252 from trial #39. This version attempts to improve by:

1. Restricting trades to the first 15 minutes of the session.
2. Using a 1-minute bar interval.
3. Defining the opening high as the highest high of the first N bars.
4. Entering short when price closes below the opening high after having
   made a new high, with a bearish reversal bar.
5. Using a fixed risk based on the opening range.
6. Adding a minimum opening range filter.
7. Requiring the reversal bar to close below the prior bar's low.
8. Adding a minimum time filter (at least 3 bars).
"""


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "1m",
        opening_minutes: int = 15,
        min_bars: int = 3,
        risk_mult: float = 0.5,
        min_range: float = 2.0,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.min_bars = min_bars
        self.risk_mult = risk_mult
        self.min_range = min_range
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

        if ctx.session != self._session:
            self._session = ctx.session
            self._traded_this_session = False
            self._opening_high = None
            self._bars_seen = 0

        if self._traded_this_session:
            return

        session_elapsed = (ctx.ts - ctx.session_start) / 60_000_000_000
        if session_elapsed > self.opening_minutes:
            return

        if self.interval not in ctx.bars:
            return

        history = ctx.history(self.interval)
        if len(history) < 2:
            return

        last = history[-1]
        self._bars_seen += 1

        if self._bars_seen < self.min_bars:
            if self._opening_high is None or last.high > self._opening_high:
                self._opening_high = last.high
            return

        if last.high > self._opening_high:
            self._opening_high = last.high

        # Wait for a bar that closes below the prior bar's low, confirming
        # a failed breakout.
        prior = history[-2]
        if last.close >= prior.low:
            return

        opening_range = self._opening_high - min(
            b.low for b in history.last(self._bars_seen)
        )
        if opening_range < self.min_range:
            return

        risk = self.risk_mult * opening_range
        if risk <= 0:
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
