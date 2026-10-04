"""Short-side opening reversal strategy for NQ futures, v7.

This version simplifies the signal and focuses on the first 5 minutes of the
session, where the reversal effect is hypothesized to be strongest. It uses a
5-minute bar to capture the opening range in one bar and enters when a
subsequent bar closes below the opening bar's high after having made a higher
high. The risk is half the distance from the entry to the opening high, with a
symmetric target.
"""


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        opening_minutes: int = 5,
        lookback: int = 2,
        risk_mult: float = 0.5,
        min_range: float = 1.0,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.lookback = lookback
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

        # First bar establishes the opening high.
        if self._bars_seen == 1:
            self._opening_high = last.high
            return

        # Need at least one more bar to see a reversal.
        if self._bars_seen < self.lookback:
            if last.high > self._opening_high:
                self._opening_high = last.high
            return

        # Update opening high if the current bar makes a new high.
        if last.high > self._opening_high:
            self._opening_high = last.high

        # Signal: price made a new high (or held the opening high) and then
        # closed below it.
        if last.close >= self._opening_high:
            return

        # Require a minimum opening range to filter quiet sessions.
        opening_low = min(b.low for b in history.last(self._bars_seen))
        opening_range = self._opening_high - opening_low
        if opening_range < self.min_range:
            return

        # Risk is a fraction of the distance from the high to the close.
        risk = self.risk_mult * (self._opening_high - last.close)
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
