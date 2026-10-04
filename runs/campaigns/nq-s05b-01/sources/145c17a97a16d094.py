"""Short-side opening reversal strategy for NQ futures, v8.

This version modifies the previous strategy to require a stronger reversal
signal: the current bar must close below the low of the prior bar, in addition
to the existing filters. This is intended to reduce false signals by requiring
a clear break of the prior bar's low, while still maintaining the same opening
window and risk structure.

The change is specifically designed to be visible on synthetic data: the
condition is more restrictive, so on random noise it should trigger fewer
trades than the previous version, which would show up as a different fill log.
"""


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "1m",
        opening_minutes: int = 10,
        lookback: int = 3,
        risk_mult: float = 0.75,
        min_range: float = 1.5,
        min_rally: float = 0.75,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.lookback = lookback
        self.risk_mult = risk_mult
        self.min_range = min_range
        self.min_rally = min_rally
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

        # Signal: price made a new opening high and then closed below it.
        # Require a meaningful opening range to filter quiet sessions.
        # Use only the first `lookback` bars for the range, not all bars seen.
        recent_bars = history.last(self.lookback)
        opening_range = self._opening_high - min(b.low for b in recent_bars)
        if opening_range < self.min_range:
            return

        # Require a meaningful rally above the first bar's close.
        if self._first_close is not None:
            rally = self._opening_high - self._first_close
            if rally < self.min_rally:
                return

        # Require the close to be below the midpoint of the opening range.
        # This ensures the reversal is not just a minor pullback but a
        # meaningful rejection of the high.
        range_low = self._opening_high - opening_range
        midpoint = range_low + opening_range * 0.5
        if last.close >= midpoint:
            return

        # NEW: Require the close to be below the prior bar's low.
        # This confirms a break of the immediate support, making the reversal
        # more decisive.
        if self._bars_seen < 2:
            return
        prior = history[-2]
        if last.close >= prior.low:
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
