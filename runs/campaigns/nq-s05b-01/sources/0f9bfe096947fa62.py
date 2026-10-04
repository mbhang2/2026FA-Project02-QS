"""Short-side opening reversal strategy for NQ futures, v7.

This version targets the highest-scoring prior variants (#31, #39, #90) while
reducing the number of trades to avoid overtrading and commission drag. The
key changes from the current strategy:

1. Use a 5-minute bar instead of 1-minute to reduce noise and commission.
2. Enter only on the first bar that closes below the opening high after a
   rally, but require the reversal to happen within the first 30 minutes.
3. Require a larger opening range and rally to filter for meaningful moves.
4. Use a fixed risk based on the opening range, capped to avoid huge stops.
5. Only one trade per session, with a strict session-end exit.
"""


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        opening_minutes: int = 30,
        lookback: int = 3,
        min_range: float = 3.0,
        min_rally: float = 2.0,
        risk_mult: float = 0.5,
        max_risk: float = 6.0,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.lookback = lookback
        self.min_range = min_range
        self.min_rally = min_rally
        self.risk_mult = risk_mult
        self.max_risk = max_risk
        self.size = size

        self._session = None
        self._traded_this_session = False
        self._opening_high = None
        self._bars_seen = 0
        self._first_close = None

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
            self._first_close = None

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

        # Track the first bar's close for the rally filter.
        if self._bars_seen == 1:
            self._first_close = last.close

        # Need enough bars to establish an opening high.
        if self._bars_seen < self.lookback:
            if self._opening_high is None or last.high > self._opening_high:
                self._opening_high = last.high
            return

        # Update opening high with the current bar.
        if last.high > self._opening_high:
            self._opening_high = last.high

        # Require a meaningful opening range.
        opening_low = min(b.low for b in history.last(self._bars_seen))
        opening_range = self._opening_high - opening_low
        if opening_range < self.min_range:
            return

        # Require a meaningful rally above the first bar's close.
        if self._first_close is not None:
            rally = self._opening_high - self._first_close
            if rally < self.min_rally:
                return

        # Signal: the current bar closes below the opening high after a rally.
        if last.close >= self._opening_high:
            return

        # Risk is a fraction of the distance from the high to the close,
        # but capped.
        risk = self.risk_mult * (self._opening_high - last.close)
        if risk <= 0:
            return
        if risk > self.max_risk:
            risk = self.max_risk

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
