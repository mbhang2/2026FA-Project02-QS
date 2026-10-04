"""Short-side opening reversal strategy for NQ futures, v8.

Prior trials show that a simple opening-range reversal can be profitable,
with the best score +0.1252 from #39. This version attempts to refine the
entry by focusing on a very specific setup: a strong opening rally that
fails at a clear resistance level, followed by a decisive bearish
engulfing bar. We use a 2-minute bar to reduce noise, require a minimum
opening range, and use a fixed risk based on the opening range rather
than the bar's own range, to better capture the reversal potential.

Changes from v7: fixed mypy error by explicitly checking for None before
subtracting opening_low from opening_high.
"""


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "2m",
        opening_minutes: int = 20,
        lookback: int = 4,
        risk_mult: float = 0.5,
        min_range: float = 3.0,
        min_rally: float = 1.5,
        body_ratio: float = 0.6,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.lookback = lookback
        self.risk_mult = risk_mult
        self.min_range = min_range
        self.min_rally = min_rally
        self.body_ratio = body_ratio
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

        # Compute opening range, but only if both high and low are set.
        if self._opening_high is None or self._opening_low is None:
            return
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
        if last.close >= last.open or body < self.body_ratio * bar_range:
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
