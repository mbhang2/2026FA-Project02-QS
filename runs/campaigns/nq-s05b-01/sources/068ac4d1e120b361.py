"""Short-side opening reversal strategy for NQ futures, v8.

This version fixes a static type error in v7 where `self._opening_high` and
`self._opening_low` could be `None` when used in arithmetic. It initializes
them to `0.0` and uses explicit checks before computing the range.

The strategy remains short-only with symmetric 1:1 risk/reward, entering
during the first 5 minutes of the session when price reverses from an opening
high with a bearish bar.
"""


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "30s",
        opening_bars: int = 10,
        min_range: float = 2.0,
        min_rally: float = 1.0,
        bearish_body_ratio: float = 0.5,
        risk_mult: float = 0.8,
        max_risk: float = 8.0,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_bars = opening_bars
        self.min_range = min_range
        self.min_rally = min_rally
        self.bearish_body_ratio = bearish_body_ratio
        self.risk_mult = risk_mult
        self.max_risk = max_risk
        self.size = size

        self._session = None
        self._traded_this_session = False
        self._opening_high = 0.0
        self._opening_low = 0.0
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
            self._opening_high = 0.0
            self._opening_low = 0.0
            self._bars_seen = 0
            self._first_close = None

        if self._traded_this_session:
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
        if self._bars_seen == 1 or last.high > self._opening_high:
            self._opening_high = last.high
        if self._bars_seen == 1 or last.low < self._opening_low:
            self._opening_low = last.low

        # Need enough bars to establish an opening range.
        if self._bars_seen < self.opening_bars:
            return

        # Only trade within the first 5 minutes (10 bars of 30s).
        if self._bars_seen > self.opening_bars:
            return

        # Signal: price made a new opening high and then closed below it.
        # Require a meaningful opening range to filter quiet sessions.
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
        if last.close >= last.open or body < self.bearish_body_ratio * bar_range:
            return

        # Risk is a fraction of the opening range, capped.
        risk = min(self.risk_mult * opening_range, self.max_risk)
        if risk <= 0:
            return

        # Enter short with symmetric stop and target.
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
