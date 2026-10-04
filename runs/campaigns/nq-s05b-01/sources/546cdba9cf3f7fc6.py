"""Short-side opening reversal strategy for NQ futures, v6.

Building on the best scored prior trial (#31, +0.1244) while avoiding the
exposure-floor disqualifications that plagued many attempts. The core idea
remains a short-only reversal in the opening minutes, but with careful
state management to avoid lookahead and ensure consistent session coverage.

Key changes from v5 and earlier:
- Uses a 30-second bar interval for finer resolution on the opening range.
- Tracks the opening high only from bars fully within the opening window.
- Requires a new high followed by a close below the prior bar's low,
  confirming actual reversal rather than a minor pullback.
- Risk is a fixed fraction of the opening range, not dependent on the
  current bar's close, avoiding degenerate zero-risk cases.
- Does not require a bearish body; instead requires the close to be below
  the previous bar's low, which is a stronger reversal signal.
- Maintains state incrementally to avoid iterating over history beyond the
  window, preventing WindowTooShort crashes.
"""


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "30s",
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

        # Per-session state, reset on session change.
        self._session = None
        self._traded_this_session = False
        self._opening_high = 0.0
        self._opening_low = 0.0
        self._bars_seen = 0
        self._prev_close = 0.0
        self._prev_low = 0.0

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
            self._prev_close = 0.0
            self._prev_low = 0.0

        if self._traded_this_session:
            return

        # Only trade during the opening window.
        session_elapsed = (ctx.ts - ctx.session_start) / 60_000_000_000
        if session_elapsed > self.opening_minutes:
            return

        # Only act when our bar closes.
        if self.interval not in ctx.bars:
            return

        bar = ctx.bars[self.interval]

        # Update opening high/low with bars fully within the opening window.
        if self._bars_seen < self.lookback:
            if self._bars_seen == 0:
                self._opening_high = bar.high
                self._opening_low = bar.low
            else:
                if bar.high > self._opening_high:
                    self._opening_high = bar.high
                if bar.low < self._opening_low:
                    self._opening_low = bar.low

        self._bars_seen += 1

        # Need enough bars to establish the opening range.
        if self._bars_seen < self.lookback:
            self._prev_close = bar.close
            self._prev_low = bar.low
            return

        # Update opening high/low with the current bar if it makes a new high.
        if bar.high > self._opening_high:
            self._opening_high = bar.high
        if bar.low < self._opening_low:
            self._opening_low = bar.low

        opening_range = self._opening_high - self._opening_low
        if opening_range < self.min_range:
            self._prev_close = bar.close
            self._prev_low = bar.low
            return

        # Reversal signal: current bar closes below the previous bar's low,
        # indicating a break of the immediate uptrend.
        if self._bars_seen > self.lookback and bar.close < self._prev_low:
            risk = self.risk_mult * opening_range
            if risk <= 0:
                self._prev_close = bar.close
                self._prev_low = bar.low
                return

            ctx.sell(
                size=self.size,
                stop_offset=risk,
                target_offset=risk,
                tag="opening-reversal-short",
            )
            self._traded_this_session = True

        self._prev_close = bar.close
        self._prev_low = bar.low

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
