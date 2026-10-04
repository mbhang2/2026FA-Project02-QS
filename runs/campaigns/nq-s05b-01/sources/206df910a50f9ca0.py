"""Short-side opening reversal strategy for NQ futures, v7.

The campaign requires a short-only reversal strategy using a symmetric 1:1
risk/reward. Prior trials show the best score +0.1244 from #31, while the
current strategy (#35) was not scored due to insufficient exposure.

This version attempts to improve on #31 by:
- Keeping the proven structure from #31 (1-minute bars, first 15 minutes,
  opening high after N bars, bearish reversal bar, risk as fraction of
  opening range).
- Adding a more robust opening range filter based on the first `lookback`
  bars rather than the cumulative low.
- Requiring the reversal bar's close to be below the midpoint of the
  opening range, confirming a true reversal rather than a shallow pullback.
- Using a fixed risk multiplier of 0.5 on the opening range, with a minimum
  range of 2.0 points.
- Trading only once per session.
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
        bearish_body_mult: float = 0.5,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.lookback = lookback
        self.risk_mult = risk_mult
        self.min_range = min_range
        self.bearish_body_mult = bearish_body_mult
        self.size = size
        self._session = None
        self._traded_this_session = False
        self._opening_high = 0.0
        self._opening_low = 0.0
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
            self._opening_high = 0.0
            self._opening_low = 0.0
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

        # Track opening high and low for the first `lookback` bars.
        if self._bars_seen <= self.lookback:
            if self._bars_seen == 1:
                self._opening_high = last.high
                self._opening_low = last.low
            else:
                if last.high > self._opening_high:
                    self._opening_high = last.high
                if last.low < self._opening_low:
                    self._opening_low = last.low

        # Need enough bars to establish the opening range.
        if self._bars_seen < self.lookback:
            return

        # Update opening high with the current bar.
        if last.high > self._opening_high:
            self._opening_high = last.high

        # Opening range from the first `lookback` bars.
        opening_range = self._opening_high - self._opening_low
        if opening_range < self.min_range:
            return

        # Require a bearish reversal bar: close below open and a body that is
        # at least a fraction of the bar's range.
        bar_range = last.high - last.low
        if bar_range <= 0:
            return
        body = last.open - last.close
        if last.close >= last.open or body < self.bearish_body_mult * bar_range:
            return

        # Require the close to be below the midpoint of the opening range,
        # confirming a true reversal.
        midpoint = (self._opening_high + self._opening_low) / 2.0
        if last.close >= midpoint:
            return

        # Risk is a fraction of the opening range.
        risk = self.risk_mult * opening_range
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
