"""Short-side opening reversal strategy for NQ futures, v8.

The campaign requires a short-only reversal strategy using a symmetric 1:1
risk/reward. Prior trials have been mostly negative, with the best score
+0.1252 from trial #39. This version attempts to improve by:

1. Restricting trades to the first 20 minutes of the session, where the
   paper found the strongest effect.
2. Using a 1-minute bar interval to reduce noise while still allowing
   early entry.
3. Defining the opening high as the highest high of the first N bars,
   and entering short when price closes below that high after having
   made a new high (a reversal confirmation).
4. Using a fixed risk based on the opening range rather than an ATR,
   to keep the structure simple and symmetric.
5. Adding a minimum opening range filter to avoid trading in very quiet
   sessions where the reversal signal is likely noise.
6. Requiring the reversal bar to be a relatively strong down bar,
   indicating actual selling pressure rather than a minor pullback.
7. Only trading if the opening high is above the first bar's close by
   at least a minimum amount, to ensure a meaningful rally to reverse.
8. Adding a time-based filter: only trade if the reversal occurs after
   at least 3 bars but before 20 minutes, to avoid both too-early and
   too-late signals.
9. Requiring that the reversal bar's low is below the opening high by
   at least the risk distance, ensuring the stop is not too tight.
"""


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "1m",
        opening_minutes: int = 20,
        lookback: int = 5,
        risk_mult: float = 0.5,
        min_range: float = 2.0,
        bearish_body_mult: float = 0.5,
        min_rally: float = 1.0,
        min_bars_before_entry: int = 3,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.lookback = lookback
        self.risk_mult = risk_mult
        self.min_range = min_range
        self.bearish_body_mult = bearish_body_mult
        self.min_rally = min_rally
        self.min_bars_before_entry = min_bars_before_entry
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

        # Require a minimum number of bars before entry.
        if self._bars_seen < self.min_bars_before_entry:
            return

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

        # Risk is a fraction of the distance from the high to the close.
        risk = self.risk_mult * (self._opening_high - last.close)
        if risk <= 0:
            return

        # Additional filter: the reversal bar must trade below the opening high
        # by at least the risk distance, ensuring the stop is not too tight.
        if last.low > self._opening_high - risk:
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
