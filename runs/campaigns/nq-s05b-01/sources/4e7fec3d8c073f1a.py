"""Short-side opening reversal strategy for NQ futures, v7.

Prior trials show a faint positive edge for short-only opening reversals,
with the best score +0.1252 from trial #39. This version attempts to
refine the signal by using a 30-second bar interval for finer resolution
during the opening minutes, and by entering on a close below the opening
high after a strong up-move, with a symmetric risk/reward based on the
opening range.

Key changes from v6:
- 30-second bars instead of 1-minute, allowing earlier and more precise
  entry within the opening window.
- Opening window extended to 20 minutes to capture more of the early
  session reversal while still avoiding the midday noise.
- Opening high is tracked over the first 8 bars, and the short entry
  requires a close below that high after the high was made.
- Risk is a fixed fraction of the opening range, with a minimum range
  filter to avoid quiet sessions.
- The reversal bar must close below its open, indicating selling pressure.
"""


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "30s",
        opening_minutes: int = 20,
        lookback: int = 8,
        risk_mult: float = 0.5,
        min_range: float = 1.5,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.lookback = lookback
        self.risk_mult = risk_mult
        self.min_range = min_range
        self.size = size

        self._session = None
        self._traded_this_session = False
        self._opening_high = None
        self._opening_low = None
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
            self._opening_low = None
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

        # Track opening high and low.
        if self._opening_high is None or last.high > self._opening_high:
            self._opening_high = last.high
        if self._opening_low is None or last.low < self._opening_low:
            self._opening_low = last.low

        # Need enough bars to establish an opening range.
        if self._bars_seen < self.lookback:
            return

        # Require a meaningful opening range.
        if self._opening_high is None or self._opening_low is None:
            return
        opening_range = self._opening_high - self._opening_low
        if opening_range < self.min_range:
            return

        # Signal: price made a new opening high and then closed below it,
        # with the reversal bar closing below its open.
        if last.close >= last.open:
            return
        if last.close >= self._opening_high:
            return

        # Risk is a fraction of the opening range.
        risk = self.risk_mult * opening_range
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
