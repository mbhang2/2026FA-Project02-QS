"""Short-side opening reversal strategy for NQ futures, v7.

This is a refinement of the current strategy, aiming to improve upon the +0.1244
score from trial #31. The core idea remains: sell short after an opening rally
reverses, with a symmetric 1:1 risk/reward. Changes from v6:

1. Use a 30-second bar interval instead of 1-minute, allowing faster entry
   and more precise detection of the reversal.
2. Increase the opening window to 20 minutes to capture more sessions where
   the reversal develops slightly later.
3. Reduce the lookback to 3 bars to trigger earlier after a new high.
4. Use a tighter bearish body requirement (0.3 instead of 0.5) to allow
   smaller reversal bars.
5. Add a check that the current bar's close is below the previous bar's low,
   confirming a break of the immediate low.
6. Require a minimum number of bars (at least 2) before trading.
7. Keep all other parameters from v6 as defaults.
"""


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "30s",
        opening_minutes: int = 20,
        lookback: int = 3,
        risk_mult: float = 0.5,
        min_range: float = 1.5,
        bearish_body_mult: float = 0.3,
        min_rally: float = 0.75,
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
        self._prev_low = None

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
            self._prev_low = None

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
            self._prev_low = last.low
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
            self._prev_low = last.low
            return

        # Require a meaningful rally above the first bar's close.
        if self._first_close is not None:
            rally = self._opening_high - self._first_close
            if rally < self.min_rally:
                self._prev_low = last.low
                return

        # Require a bearish reversal bar: close below open and a body that is
        # at least a fraction of the bar's range.
        bar_range = last.high - last.low
        if bar_range <= 0:
            self._prev_low = last.low
            return
        body = last.open - last.close
        if last.close >= last.open or body < self.bearish_body_mult * bar_range:
            self._prev_low = last.low
            return

        # Require the close to be below the previous bar's low (break of low).
        if self._prev_low is not None and last.close >= self._prev_low:
            self._prev_low = last.low
            return

        # Risk is a fraction of the distance from the high to the close.
        risk = self.risk_mult * (self._opening_high - last.close)
        if risk <= 0:
            self._prev_low = last.low
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
