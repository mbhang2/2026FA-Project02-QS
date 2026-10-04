"""Short-side opening reversal strategy for NQ futures, v7.

The campaign requires a short-only reversal strategy using a symmetric 1:1
risk/reward. Prior trials have been mostly negative, with the best score
+0.1252 from trial #39. This version attempts to improve by simplifying the
entry conditions to increase exposure while retaining the core reversal idea.

Key changes from v6:
- Removed the minimum opening range filter (min_range=0) to trade in more
  sessions.
- Removed the minimum rally filter (min_rally=0) to allow trades even when
  the rally above the first bar's close is small.
- Removed the bearish body requirement (bearish_body_mult=0) to allow any
  down close after a new high to trigger a short.
- Reduced lookback to 3 bars to establish the opening high earlier.
- Kept risk_mult=0.5, so the stop/target is half the distance from the
  opening high to the entry close, providing a reasonable risk/reward.
- Added a minimum risk of 0.5 points to avoid tiny stops that are likely
  noise.

These changes should increase the number of trades per session, ensuring
exposure in at least 70% of sessions as required by the campaign, while
still capturing the reversal pattern.
"""


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "1m",
        opening_minutes: int = 15,
        lookback: int = 3,
        risk_mult: float = 0.5,
        min_risk: float = 0.5,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.lookback = lookback
        self.risk_mult = risk_mult
        self.min_risk = min_risk
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
        if len(history) < 1:
            return

        last = history[-1]
        self._bars_seen += 1

        # Update opening high with the current bar.
        if self._opening_high is None or last.high > self._opening_high:
            self._opening_high = last.high

        # Need enough bars to establish an opening high.
        if self._bars_seen < self.lookback:
            return

        # Signal: price made a new opening high and then closed below it.
        # No additional filters; any down close after a new high triggers.
        if last.close >= self._opening_high:
            return

        # Risk is a fraction of the distance from the high to the close.
        risk = self.risk_mult * (self._opening_high - last.close)
        if risk < self.min_risk:
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
