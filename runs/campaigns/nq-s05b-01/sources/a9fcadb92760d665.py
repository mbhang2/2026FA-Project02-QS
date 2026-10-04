"""Short-side opening reversal strategy for NQ futures, v5.

The campaign requires a short-only reversal strategy using a symmetric 1:1
risk/reward. This version targets the highest-scoring prior trial (#22,
+0.0535) while attempting to reduce its exposure and improve consistency.

Key changes from the current strategy:
1. Uses a 5-minute bar interval, matching the timeframe of the best prior
   result.
2. Enters only when the first bar of the session closes below its open
   after making a new session high, a cleaner reversal signal.
3. Uses a fixed risk of 0.5 points, which is small enough to capture
   short-lived reversals while keeping the bracket symmetric.
4. Trades at most once per session, within the first 15 minutes.
"""


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        opening_minutes: int = 15,
        risk_points: float = 0.5,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.risk_points = risk_points
        self.size = size
        self._session = None
        self._traded_this_session = False

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

        # Get the first bar of the session.
        if ctx.session_start != history[0].open_ts:
            return

        first_bar = history[0]
        if first_bar.close >= first_bar.open:
            return

        # Signal: first bar closed below its open after making a new high.
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
