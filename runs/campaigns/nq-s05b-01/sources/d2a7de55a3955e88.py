"""Short-side opening reversal strategy for NQ futures, v7.

This version simplifies the entry logic compared to v6. It uses 5-minute bars
to reduce noise, defines the opening high as the highest high of the first
three bars (15 minutes), and enters short when a subsequent bar within the
first 30 minutes closes below that high. The trade uses a symmetric 1:1
risk/reward with a fixed 5-point stop and target. A minimum opening range
filter avoids quiet sessions. The strategy trades at most once per session.

Rationale: The paper's effect is strongest in the earliest part of the
session. Using 5-minute bars smooths out the noise of 1-minute bars while
still capturing the opening dynamics. Entering on a close below the opening
high is a simple reversal confirmation that does not require a strongly
bearish bar, increasing the number of trades and exposure. The fixed
risk/reward keeps the structure symmetric as required.
"""


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        opening_bars: int = 6,       # 30 minutes
        lookback_bars: int = 3,      # 15 minutes for opening high/low
        min_range: float = 2.0,      # minimum opening range in points
        risk_points: float = 5.0,    # symmetric stop and target distance
        size: int = 1,
    ):
        self.interval = interval
        self.opening_bars = opening_bars
        self.lookback_bars = lookback_bars
        self.min_range = min_range
        self.risk_points = risk_points
        self.size = size

        # Per-session state
        self._session = None
        self._traded_this_session = False
        self._bars_seen = 0
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._opening_high: float | None = None
        self._opening_low: float | None = None

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
            self._highs = []
            self._lows = []
            self._opening_high = None
            self._opening_low = None

        if self._traded_this_session:
            return

        # Only act on our bar interval
        if self.interval not in ctx.bars:
            return

        bar = ctx.bars[self.interval]
        self._bars_seen += 1

        # Collect first lookback_bars to compute opening high/low
        if self._bars_seen <= self.lookback_bars:
            self._highs.append(bar.high)
            self._lows.append(bar.low)
            if self._bars_seen == self.lookback_bars:
                self._opening_high = max(self._highs)
                self._opening_low = min(self._lows)
                # Range filter: skip session if opening range too small
                if self._opening_high is not None and self._opening_low is not None:
                    if self._opening_high - self._opening_low < self.min_range:
                        self._traded_this_session = True  # mark as done, no trade
            return

        # After lookback, within opening window, check for reversal
        if self._bars_seen <= self.opening_bars:
            if self._opening_high is not None and bar.close < self._opening_high:
                # Sell short with symmetric stop and target
                ctx.sell(
                    size=self.size,
                    stop_offset=self.risk_points,
                    target_offset=self.risk_points,
                    tag="opening-reversal-short",
                )
                self._traded_this_session = True
                return

        # If we've passed the opening window without trading, mark as done
        if self._bars_seen > self.opening_bars:
            self._traded_this_session = True

    def on_fill(self, ctx) -> None:
        pass

    def on_lifecycle(self, ctx) -> None:
        pass
