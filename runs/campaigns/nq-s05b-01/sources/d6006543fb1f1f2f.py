"""Short-side opening reversal strategy for NQ futures, v7.

This version uses a limit order to sell at the opening high, rather than
entering on a market order after a bearish bar. The idea is to get a better
entry price (at the high) and avoid slippage. The strategy:

1. Uses 1-minute bars.
2. Defines the opening high as the highest high of the first `lookback` bars
   (default 5 bars = 5 minutes).
3. After those bars, if the current price is below the opening high, places a
   sell limit order at that high with a symmetric stop and target offset.
4. The offset is a fraction (`risk_mult`) of the opening range.
5. The order is GTD and expires at the end of the opening window
   (`opening_minutes`), so it is automatically cancelled if not filled.
6. Only one trade per session is allowed.

The campaign requires short-only, reversal (sell after a rise), and symmetric
1:1 risk/reward. This strategy satisfies all three.
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
        min_rally: float = 1.0,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.lookback = lookback
        self.risk_mult = risk_mult
        self.min_range = min_range
        self.min_rally = min_rally
        self.size = size

        # Per-session state
        self._session = None
        self._traded_this_session = False
        self._bars_seen = 0
        self._opening_high = None
        self._opening_low = None
        self._first_close = None

    def setup(self, ctx) -> None:
        ctx.subscribe(self.interval)

    def on_bars(self, ctx) -> None:
        if not ctx.actionable:
            return
        if ctx.position or ctx.working_order_ids:
            return

        # Reset state on session change
        if ctx.session != self._session:
            self._session = ctx.session
            self._traded_this_session = False
            self._bars_seen = 0
            self._opening_high = None
            self._opening_low = None
            self._first_close = None

        if self._traded_this_session:
            return

        # Only trade during the opening window
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

        # Track first bar's close for rally filter
        if self._bars_seen == 1:
            self._first_close = last.close

        # Update opening high/low
        if self._opening_high is None or last.high > self._opening_high:
            self._opening_high = last.high
        if self._opening_low is None or last.low < self._opening_low:
            self._opening_low = last.low

        # Need enough bars to establish the opening high
        if self._bars_seen < self.lookback:
            return

        # Ensure all required values are set (mypy safety)
        if self._opening_high is None or self._opening_low is None or self._first_close is None:
            return

        # Filters
        opening_range = self._opening_high - self._opening_low
        if opening_range < self.min_range:
            return

        rally = self._opening_high - self._first_close
        if rally < self.min_rally:
            return

        # Only place a limit order if current price is below the opening high
        if last.close >= self._opening_high:
            return

        # Symmetric offset based on opening range
        offset = self.risk_mult * opening_range
        if offset <= 0:
            return

        # Place a sell limit at the opening high, with GTD expiry at end of window
        expire_ts = ctx.session_start + self.opening_minutes * 60_000_000_000
        ctx.sell(
            size=self.size,
            type="limit",
            limit=self._opening_high,
            stop_offset=offset,
            target_offset=offset,
            tif="GTD",
            expire_ts=expire_ts,
            tag="opening-reversal-short-limit",
        )
        self._traded_this_session = True

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
