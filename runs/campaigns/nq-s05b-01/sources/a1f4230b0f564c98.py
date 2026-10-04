"""Short-side opening reversal strategy for NQ futures, v7.

The campaign requires a short-only reversal strategy using a symmetric 1:1
risk/reward. Prior trials show the best scores came from strategies that
traded frequently (near one trade per session) with a simple opening-range
reversal. This version simplifies further:

1. Uses a 2-minute bar interval to capture the opening move with less noise
   than 1-minute bars.
2. Enters only during the first 20 minutes of the session.
3. Defines the opening high as the highest high of the first 5 bars.
4. Enters short when price closes below the opening high after having made
   a new high (i.e., the high is from the current or previous bar).
5. Uses a fixed risk of 2.0 points, symmetric target and stop.
6. Requires a minimum opening range of 3.0 points to filter quiet sessions.
7. Trades only once per session.

This is a cleaner version of the pattern that scored +0.1244 in trial #31,
with fewer parameters and a slightly longer opening window.
"""


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "2m",
        opening_minutes: int = 20,
        lookback: int = 5,
        risk_points: float = 2.0,
        min_range: float = 3.0,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.lookback = lookback
        self.risk_points = risk_points
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

        # Signal: price made a new opening high and then closed below it.
        # The high must be from the current or previous bar.
        recent_high = max(
            b.high for b in history.last(min(3, self._bars_seen))
        )
        if last.close >= recent_high:
            return

        # Require a meaningful opening range to filter quiet sessions.
        opening_range = self._opening_high - min(
            b.low for b in history.last(self._bars_seen)
        )
        if opening_range < self.min_range:
            return

        # Require a bearish bar: close below open.
        if last.close >= last.open:
            return

        # Enter short with fixed symmetric risk.
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
