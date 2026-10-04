"""Short-side opening reversal strategy for NQ futures, v7.

The campaign requires a short-only reversal strategy with a symmetric 1:1
risk/reward. Trial #31 scored +0.1244, the best so far. This version builds on
that while avoiding the static type error from v6.

Changes from v6:
- Fix the mypy error by initializing `_opening_high` to 0.0 instead of None.
- Keep the 1-minute bar interval and 15-minute opening window.
- Keep the confirmed reversal logic: close below the opening high by a small
  threshold, with a bearish bar and opening-range filters.
- Keep the session gap filter and risk cap.
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
        max_range: float = 12.0,
        bearish_body_mult: float = 0.5,
        close_below_high: float = 0.25,
        max_gap: float = 8.0,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.lookback = lookback
        self.risk_mult = risk_mult
        self.min_range = min_range
        self.max_range = max_range
        self.bearish_body_mult = bearish_body_mult
        self.close_below_high = close_below_high
        self.max_gap = max_gap
        self.size = size
        self._session = None
        self._traded_this_session = False
        self._opening_high = 0.0
        self._bars_seen = 0
        self._session_open_price = None

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
            self._bars_seen = 0
            self._session_open_price = None

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

        # Record the first bar's open as the session open price.
        if self._session_open_price is None:
            self._session_open_price = last.open

        # Need enough bars to establish an opening high.
        if self._bars_seen < self.lookback:
            if last.high > self._opening_high:
                self._opening_high = last.high
            return

        # Update opening high with the current bar.
        if last.high > self._opening_high:
            self._opening_high = last.high

        # Skip sessions with a large opening gap: these tend to trend.
        gap = abs(last.open - self._session_open_price)
        if gap > self.max_gap:
            return

        # Signal: price made a new opening high and then closed below it.
        # Require a meaningful opening range to filter quiet sessions.
        opening_range = self._opening_high - min(
            b.low for b in history.last(self._bars_seen)
        )
        if opening_range < self.min_range:
            return
        if opening_range > self.max_range:
            return

        # Require a bearish reversal bar: close below open and a body that is
        # at least a fraction of the bar's range.
        bar_range = last.high - last.low
        if bar_range <= 0:
            return
        body = last.open - last.close
        if last.close >= last.open or body < self.bearish_body_mult * bar_range:
            return

        # Require close to be below the opening high by a small margin.
        if last.close > self._opening_high - self.close_below_high:
            return

        # Risk is a fraction of the distance from the high to the close,
        # capped to avoid oversized risk.
        risk = self.risk_mult * (self._opening_high - last.close)
        if risk <= 0:
            return
        risk = min(risk, 6.0)

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
