"""Short-side opening reversal strategy for NQ futures, v8.

This version returns to the 1-minute bar interval and extends the opening
window to 20 minutes. It uses a rolling lookback of 6 bars to establish the
opening high and requires a close below the opening high with a bearish bar.
The risk is a fixed 2.0 points, with a symmetric target, to avoid the
variability of a dynamic risk that has been too small to overcome commission.
"""


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "1m",
        opening_minutes: int = 20,
        lookback: int = 6,
        risk_points: float = 2.0,
        min_range: float = 1.5,
        bearish_body_mult: float = 0.4,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.lookback = lookback
        self.risk_points = risk_points
        self.min_range = min_range
        self.bearish_body_mult = bearish_body_mult
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

        # Update opening high with the current bar.
        if self._opening_high is None or last.high > self._opening_high:
            self._opening_high = last.high

        # Need enough bars to establish a meaningful opening range.
        if self._bars_seen < self.lookback:
            return

        # Signal: price made a new high (or held the opening high) and then
        # closed below it with a bearish bar.
        if last.close >= self._opening_high:
            return

        # Require a bearish reversal bar: close below open and a body that is
        # at least a fraction of the bar's range.
        bar_range = last.high - last.low
        if bar_range <= 0:
            return
        body = last.open - last.close
        if last.close >= last.open or body < self.bearish_body_mult * bar_range:
            return

        # Require a minimum opening range to filter quiet sessions.
        opening_low = min(b.low for b in history.last(self._bars_seen))
        opening_range = self._opening_high - opening_low
        if opening_range < self.min_range:
            return

        # Fixed risk and target for a symmetric 1:1 structure.
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
