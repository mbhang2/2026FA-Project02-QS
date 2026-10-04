"""Short-side opening reversal strategy for NQ futures, v6.

This version builds on the prior successful trials (#31, score +0.1244) and
attempts to improve by refining the entry logic while keeping the required
short-only, symmetric 1:1 risk/reward structure.

Key changes from prior versions:
- Uses a 5-minute bar interval to reduce noise and transaction costs.
- Defines the opening window as the first 30 minutes of the session.
- Tracks the opening high dynamically, but requires a minimum number of bars
  before allowing a trade.
- Entry signal: price makes a new opening high, then closes below the prior
  bar's low, indicating a reversal.
- Adds a volume filter to avoid low-liquidity moves.
- Risk is a fraction of the distance from the opening high to the entry close,
  with a minimum risk to avoid overly tight stops.
"""


class Strategy:
    def __init__(
        self,
        *,
        interval: str = "5m",
        opening_minutes: int = 30,
        min_bars: int = 3,
        risk_mult: float = 0.6,
        min_risk: float = 2.0,
        min_volume: int = 500,
        size: int = 1,
    ):
        self.interval = interval
        self.opening_minutes = opening_minutes
        self.min_bars = min_bars
        self.risk_mult = risk_mult
        self.min_risk = min_risk
        self.min_volume = min_volume
        self.size = size

        self._session = None
        self._traded_this_session = False
        self._opening_high = None
        self._bars_seen = 0
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
            self._bars_seen = 0
            self._prior_low = None

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

        # Update opening high.
        if self._opening_high is None or last.high > self._opening_high:
            self._opening_high = last.high

        # Need enough bars before we can trade.
        if self._bars_seen < self.min_bars:
            self._prior_low = last.low
            return

        # Signal: price made a new opening high on the prior bar, and now
        # closes below the prior bar's low.
        if self._prior_low is None or last.close >= self._prior_low:
            self._prior_low = last.low
            return

        # Volume filter: the reversal bar should have sufficient volume.
        if last.volume < self.min_volume:
            self._prior_low = last.low
            return

        # Risk is a fraction of the distance from the opening high to the
        # entry close, with a minimum.
        risk = max(self.min_risk, self.risk_mult * (self._opening_high - last.close))
        if risk <= 0:
            self._prior_low = last.low
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
