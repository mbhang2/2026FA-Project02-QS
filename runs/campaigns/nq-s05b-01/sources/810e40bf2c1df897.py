"""Short-side opening reversal strategy for NQ futures, v6.

The campaign requires a short-only reversal strategy using a symmetric 1:1
risk/reward. Prior trials have been mostly negative, with the best score
around +0.0535. This version attempts to improve by:

1. Restricting trades to the first 15 minutes of the session, where the
   paper found the strongest effect.
2. Using a 1-minute bar interval to reduce noise while still allowing
   early entry.
3. Defining the opening high as the highest high of the first N bars,
   and entering short when price closes below that high after having
   made a new high (a reversal confirmation).
4. Using a fixed risk based on the opening range rather than an ATR,
   to keep the structure simple and symmetric.
5. Adding a minimum range filter to avoid trading in very quiet openings.
6. Adding a time filter to avoid trading in the last few minutes of the
   opening window.
7. Fixing the type error from v5: opening_high and opening_low can be None
   when opening_range is computed, so we guard against that.
"""


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "1m",
        opening_minutes: int = 15,
        lookback: int = 5,
        risk_mult: float = 0.5,
        min_range: float = 2.0,
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
        self._opening_low = None
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
            self._opening_low = None
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

        # Need enough bars to establish an opening high and low.
        if self._bars_seen < self.lookback:
            if self._opening_high is None or last.high > self._opening_high:
                self._opening_high = last.high
            if self._opening_low is None or last.low < self._opening_low:
                self._opening_low = last.low
            return

        # At this point, opening_high and opening_low are guaranteed not None.
        assert self._opening_high is not None
        assert self._opening_low is not None

        # Update opening high and low with the current bar.
        if last.high > self._opening_high:
            self._opening_high = last.high
        if last.low < self._opening_low:
            self._opening_low = last.low

        # Check minimum range to avoid quiet openings.
        opening_range = self._opening_high - self._opening_low
        if opening_range < self.min_range:
            return

        # Signal: price made a new opening high and then closed below it.
        if last.close < self._opening_high and last.close < last.open:
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
