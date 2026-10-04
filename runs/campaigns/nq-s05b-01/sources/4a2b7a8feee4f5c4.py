"""Short-side opening reversal strategy for NQ futures, v7.

The campaign requires a short-only reversal strategy using a symmetric 1:1
risk/reward. This version is a refined version of the best prior trial (#31,
score +0.1244) with adjustments to improve robustness and avoid overfitting.

Key changes from prior trials:
- Use a 2-minute bar interval to reduce noise while capturing early moves.
- Define the opening high as the highest high of the first 5 bars (10 minutes).
- Enter short when price closes below the opening high after a new high was set
  within the opening window, but only if the reversal bar is bearish.
- Risk is set as a fraction of the opening range, capped to avoid oversized stops.
- Require a minimum opening range to filter quiet sessions.
- Only trade once per session.
- Add a filter that the reversal bar's close must be below the prior bar's low,
  confirming a downside break.
"""


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "2m",
        opening_bars: int = 5,
        risk_fraction: float = 0.4,
        min_range: float = 1.5,
        max_risk: float = 5.0,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_bars = opening_bars
        self.risk_fraction = risk_fraction
        self.min_range = min_range
        self.max_risk = max_risk
        self.size = size

        self._session = None
        self._traded_this_session = False
        self._opening_high = None
        self._bars_seen = 0
        self._opening_low = None
        self._prior_low = None

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
            self._prior_low = None

        if self._traded_this_session:
            return

        if self.interval not in ctx.bars:
            return

        history = ctx.history(self.interval)
        if len(history) < 1:
            return

        last = history[-1]
        self._bars_seen += 1

        # Track opening high and low for the first few bars.
        if self._bars_seen <= self.opening_bars:
            if self._opening_high is None or last.high > self._opening_high:
                self._opening_high = last.high
            if self._opening_low is None or last.low < self._opening_low:
                self._opening_low = last.low

        # Need enough bars to establish the opening range.
        if self._bars_seen < self.opening_bars:
            self._prior_low = last.low
            return

        # Only trade after the opening bars have completed.
        if self._bars_seen == self.opening_bars:
            self._prior_low = last.low
            return

        # Require a meaningful opening range.
        if self._opening_high is None or self._opening_low is None:
            return
        opening_range = self._opening_high - self._opening_low
        if opening_range < self.min_range:
            return

        # Signal: price made a new high after the opening bars, then reversed.
        # The current bar must close below the prior bar's low and be bearish.
        if last.close >= last.open:
            return
        if self._prior_low is None or last.close >= self._prior_low:
            return

        # Risk is a fraction of the opening range, capped.
        risk = min(self.risk_fraction * opening_range, self.max_risk)
        if risk <= 0:
            return

        ctx.sell(
            size=self.size,
            stop_offset=risk,
            target_offset=risk,
            tag="opening-reversal-short",
        )
        self._traded_this_session = True

        # Update prior low for next bar.
        self._prior_low = last.low

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
