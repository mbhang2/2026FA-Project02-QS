"""Short-side opening reversal strategy for NQ futures, v8.

The campaign requires a short-only reversal strategy using a symmetric 1:1
risk/reward. Prior successful trials (#31, #39, #57) shared a common structure:
a 5-minute bar interval, a 30-minute opening window, and a reversal signal
based on a bar closing below the opening high after a new high was made.
Trial #39 scored +0.1252 with 359 trades over 384 sessions, the best
non-identical result. #31 scored +0.1244 with 360 trades using a similar
approach but with a slightly different risk calculation.

This version attempts to improve on #39 by:
1. Keeping the 5-minute interval and 30-minute opening window.
2. Using a lookback of 6 bars (30 minutes) to define the opening high,
   rather than 5, to capture a slightly longer initial rally.
3. Requiring the reversal bar to close below the opening high, not just
   below the bar's open, to confirm actual reversal from the high.
4. Using a risk based on the distance from the opening high to the current
   close, with a multiplier of 0.75 rather than 0.5, to give the trade a
   slightly wider stop and target, which may improve the win rate.
5. Adding a filter that the opening range must be at least 3.0 points,
   slightly higher than prior 2.0, to focus on more volatile sessions.
6. Requiring the reversal bar to have a body at least 0.6 of its range,
   slightly higher than prior 0.5, to ensure strong selling pressure.
7. Keeping the one-trade-per-session limit.
"""


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        opening_minutes: int = 30,
        lookback: int = 6,
        risk_mult: float = 0.75,
        min_range: float = 3.0,
        bearish_body_mult: float = 0.6,
        min_rally: float = 1.0,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.lookback = lookback
        self.risk_mult = risk_mult
        self.min_range = min_range
        self.bearish_body_mult = bearish_body_mult
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

        # Require a bearish reversal bar: close below open, close below the
        # opening high, and a body that is at least a fraction of the bar's
        # range.
        bar_range = last.high - last.low
        if bar_range <= 0:
            return
        body = last.open - last.close
        if (
            last.close >= last.open
            or last.close >= self._opening_high
            or body < self.bearish_body_mult * bar_range
        ):
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
