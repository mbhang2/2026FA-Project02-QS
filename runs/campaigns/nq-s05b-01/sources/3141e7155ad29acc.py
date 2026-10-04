"""Short-side opening reversal strategy for NQ futures, v9.

This version uses a fixed risk of 2.0 points for both stop and target,
instead of a risk multiplier based on the distance from the opening high
to the close. The fixed risk is intended to be more robust to noise in
the bar's range and to match the paper's use of a fixed point target/stop.
Other parameters are similar to v8: opening window 15 minutes, lookback 5,
minimum range 2.0, minimum rally 1.0, bearish body requirement, and a
time-based exit after 10 bars.
"""


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "1m",
        opening_minutes: int = 15,
        lookback: int = 5,
        min_range: float = 2.0,
        bearish_body_mult: float = 0.5,
        min_rally: float = 1.0,
        max_hold_bars: int = 10,
        risk_points: float = 2.0,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.lookback = lookback
        self.min_range = min_range
        self.bearish_body_mult = bearish_body_mult
        self.min_rally = min_rally
        self.max_hold_bars = max_hold_bars
        self.risk_points = risk_points
        self.size = size

        self._session = None
        self._traded_this_session = False
        self._opening_high = None
        self._bars_seen = 0
        self._first_close = None
        self._entry_bar_count = 0

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return

        # Reset per-session state on session change.
        if ctx.session != self._session:
            self._session = ctx.session
            self._traded_this_session = False
            self._opening_high = None
            self._bars_seen = 0
            self._first_close = None
            self._entry_bar_count = 0

        if self.interval not in ctx.bars:
            return

        # Time-based exit: if we have a position and have held it too long,
        # close it. We do this before the entry logic so the exit happens
        # even if the entry conditions are no longer met.
        if ctx.position:
            self._entry_bar_count += 1
            if self._entry_bar_count >= self.max_hold_bars:
                ctx.close()
            return

        if self._traded_this_session:
            return

        # Only trade during the opening window.
        session_elapsed = (ctx.ts - ctx.session_start) / 60_000_000_000
        if session_elapsed > self.opening_minutes:
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
        opening_range = self._opening_high - min(
            b.low for b in history.last(self._bars_seen)
        )
        if opening_range < self.min_range:
            return

        # Require a meaningful rally above the first bar's close.
        if self._first_close is not None:
            rally = self._opening_high - self._first_close
            if rally < self.min_rally:
                return

        # Require a bearish reversal bar: close below open and a body that is
        # at least a fraction of the bar's range.
        bar_range = last.high - last.low
        if bar_range <= 0:
            return
        body = last.open - last.close
        if last.close >= last.open or body < self.bearish_body_mult * bar_range:
            return

        # Use a fixed risk in points for both stop and target.
        risk = self.risk_points
        if risk <= 0:
            return

        ctx.sell(
            size=self.size,
            stop_offset=risk,
            target_offset=risk,
            tag="opening-reversal-short",
        )
        self._traded_this_session = True
        self._entry_bar_count = 0

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
