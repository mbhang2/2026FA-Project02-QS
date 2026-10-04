"""Short-side opening reversal strategy for NQ futures, v7.

This version simplifies the prior v6 by relaxing filters to increase trade
frequency while maintaining the core reversal idea. It uses 5-minute bars
over a 30-minute opening window, defines the opening high as the highest
high of the first 6 bars, and enters short on a bearish bar after a
meaningful rally. The risk is a fraction of the distance from the opening
high to the entry close, with a symmetric target and stop.

Changes from v6:
- Interval: 5m instead of 1m to reduce noise.
- Opening window: 30 minutes instead of 15.
- Lookback: 6 bars (30 minutes) instead of 5.
- Minimum rally: 0.5 points instead of 1.0.
- Minimum range: 0.0 (removed filter).
- Bearish body multiplier: 0.2 instead of 0.5.
These relaxations aim to trade in most sessions while retaining a reversal
edge.
"""


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        opening_minutes: int = 30,
        lookback: int = 6,
        risk_mult: float = 0.5,
        min_rally: float = 0.5,
        bearish_body_mult: float = 0.2,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.lookback = lookback
        self.risk_mult = risk_mult
        self.min_rally = min_rally
        self.bearish_body_mult = bearish_body_mult
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
