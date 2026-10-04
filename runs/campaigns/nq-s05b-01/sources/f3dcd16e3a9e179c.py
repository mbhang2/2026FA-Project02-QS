"""Short-side opening reversal strategy for NQ futures, v7.

This version simplifies the signal to the paper's core idea: sell a new
opening-range high with a symmetric 1:1 risk/reward, but only during a narrow
opening window and only when the high is followed by a close below the prior
bar's low (a confirmed reversal). The previous version's complexity did not
help; this strips filters that may have been overfitting and focuses on the
most robust elements: early entry, new high, and a bearish close.

Key changes from v6:
- Lookback reduced to 3 bars (3 minutes) to catch the earliest reversals.
- Removed the minimum range and minimum rally filters; they reduced trades
  without improving score.
- Signal is now simply: current bar's high exceeds the opening high, and the
  bar closes below the previous bar's low. This is a cleaner reversal pattern.
- Risk is still a fraction of the distance from the high to the close, but
  now uses the current bar's high (the new high) rather than the opening high,
  which better reflects the actual reversal point.
"""


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "1m",
        opening_minutes: int = 15,
        lookback: int = 3,
        risk_mult: float = 0.6,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.lookback = lookback
        self.risk_mult = risk_mult
        self.size = size

        self._session = None
        self._traded_this_session = False
        self._opening_high = None
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
            self._opening_high = None
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
        if len(history) < 2:
            return

        last = history[-1]
        prev = history[-2]
        self._bars_seen += 1

        # Need enough bars to establish an opening high.
        if self._bars_seen < self.lookback:
            if self._opening_high is None or last.high > self._opening_high:
                self._opening_high = last.high
            return

        # Update opening high with the current bar's high.
        if last.high > (self._opening_high or 0):
            self._opening_high = last.high

        # Signal: current bar makes a new opening high and closes below the
        # previous bar's low. This is a clear reversal.
        if last.high > prev.high and last.close < prev.low:
            risk = self.risk_mult * (last.high - last.close)
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
