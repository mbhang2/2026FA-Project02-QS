"""Short-side opening reversal strategy for NQ futures, v7.

This version builds on v6 but adjusts parameters to potentially improve
performance. Key changes:
- Opening window reduced to 10 minutes (paper found earliest part strongest).
- Lookback reduced to 3 bars for earlier entry.
- Risk multiplier increased to 1.0, but risk is now based on the full opening
  range rather than the distance from high to close, providing a more
  consistent risk size.
- Minimum opening range lowered to 1.0 to allow more trades.
- Bearish body requirement relaxed to 0.3 of bar range.
- Minimum rally above first close lowered to 0.5.
These changes aim to increase trade frequency while maintaining a meaningful
reversal signal, and to give each trade a larger, more symmetric risk/reward.
"""


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "1m",
        opening_minutes: int = 10,
        lookback: int = 3,
        risk_mult: float = 1.0,
        min_range: float = 1.0,
        bearish_body_mult: float = 0.3,
        min_rally: float = 0.5,
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
        self._opening_low = None
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
            self._opening_low = None
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

        # Update opening high and low.
        if self._opening_high is None or last.high > self._opening_high:
            self._opening_high = last.high
        if self._opening_low is None or last.low < self._opening_low:
            self._opening_low = last.low

        # Need enough bars to establish an opening range.
        if self._bars_seen < self.lookback:
            return

        # Ensure both opening high and low are set before subtraction.
        if self._opening_high is None or self._opening_low is None:
            return

        # Compute opening range.
        opening_range = self._opening_high - self._opening_low
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
