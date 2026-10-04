"""Short-side opening reversal strategy for NQ futures, v9.

Prior trials show the opening reversal short can be profitable when the
entry is selective. The best scores came from strategies that:
- traded only during the first 15 minutes
- required a meaningful opening range and rally
- required a bearish reversal bar

This version makes one substantive change: it reduces the minimum rally
from 1.5 to 1.0 points. The previous version's tighter filters produced
no trades on the synthetic check, but on real NQ data the 1.5-point rally
filter may be excluding many valid reversal setups. A 1.0-point rally is
still meaningful for NQ and should allow more trades while keeping the
structure intact.
"""


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "1m",
        opening_minutes: int = 15,
        lookback: int = 5,
        risk_mult: float = 0.4,
        min_range: float = 1.5,
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

        self._session: int | None = None
        self._traded_this_session = False
        self._opening_high: float | None = None
        self._opening_low: float | None = None
        self._bars_seen = 0
        self._first_close: float | None = None
        self._high_bar_index: int | None = None

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
            self._high_bar_index = None

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
            self._opening_high = last.high
            self._opening_low = last.low

        # Need enough bars to establish an opening high.
        if self._bars_seen < self.lookback:
            if self._opening_high is not None and last.high > self._opening_high:
                self._opening_high = last.high
                self._high_bar_index = self._bars_seen
            if self._opening_low is not None and last.low < self._opening_low:
                self._opening_low = last.low
            return

        # Update opening high/low with the current bar.
        if self._opening_high is not None and last.high > self._opening_high:
            self._opening_high = last.high
            self._high_bar_index = self._bars_seen
        if self._opening_low is not None and last.low < self._opening_low:
            self._opening_low = last.low

        # Only trade if the high was set within the first 10 bars.
        if self._high_bar_index is None or self._high_bar_index > 10:
            return

        # Require a meaningful opening range.
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

        # Require the reversal bar to close below the opening range midpoint.
        midpoint = (self._opening_high + self._opening_low) / 2.0
        if last.close >= midpoint:
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
