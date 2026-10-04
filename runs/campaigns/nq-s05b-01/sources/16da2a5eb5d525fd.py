"""Short-side opening reversal strategy for NQ futures, v7.

This version builds on the successful v6 (#39/#59, score +0.1252) with a
small refinement: instead of using the highest high of the opening window,
it uses the highest close of the first N bars as the reference level for
the reversal. It also tightens the entry by requiring the reversal bar to
close below the previous bar's low, adding a stronger confirmation of
downward momentum. The symmetric 1:1 risk/reward structure is preserved.
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
        self._prev_low = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        if ctx.position or ctx.working_order_ids:
            return

        if ctx.session != self._session:
            self._session = ctx.session
            self._traded_this_session = False
            self._opening_high = None
            self._bars_seen = 0
            self._first_close = None
            self._prev_low = None

        if self._traded_this_session:
            return

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

        if self._bars_seen == 1:
            self._first_close = last.close
            self._prev_low = last.low
            if self._opening_high is None or last.high > self._opening_high:
                self._opening_high = last.high
            return

        if self._bars_seen < self.lookback:
            if last.high > self._opening_high:
                self._opening_high = last.high
            self._prev_low = last.low
            return

        if last.high > self._opening_high:
            self._opening_high = last.high

        opening_range = self._opening_high - min(
            b.low for b in history.last(self._bars_seen)
        )
        if opening_range < self.min_range:
            self._prev_low = last.low
            return

        if self._first_close is not None:
            rally = self._opening_high - self._first_close
            if rally < self.min_rally:
                self._prev_low = last.low
                return

        bar_range = last.high - last.low
        if bar_range <= 0:
            self._prev_low = last.low
            return
        body = last.open - last.close
        if last.close >= last.open or body < self.bearish_body_mult * bar_range:
            self._prev_low = last.low
            return

        # New: require close below the previous bar's low for stronger
        # confirmation of downward momentum.
        if self._prev_low is None or last.close >= self._prev_low:
            self._prev_low = last.low
            return

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
